"""Capability route: verifying a route (`verify_route`).

Split out of `capability-route.py`, which re-exports every name defined here.
"""
from __future__ import annotations

import contextlib
import json
import re
from pathlib import Path

from hearting_gates import note_same_work_drift
import artifact_locator as ARTIFACT_LOCATOR
import route_format
import model_profile as PROFILE
import resource_resume as RESOURCE_RESUME
from dispatch_contract import EXECUTION_SURFACES, FALLBACK_HOPS, agent_home_equivalent

from capability_route_base import (
    _validate_output_scopes, _validate_registered_headless_evidence, _verify_selection_pins,
    DEFAULTS, DISPATCH_EVIDENCE_SCOPE_VERSION, ROOT, route_hash, TOPO, TRACKING,
    VALID_AFFINITY,
)

from capability_route_continuation import (
    _evidence_parent_dispatch_depth, _scope_touches_spec, _single_owner_nodes,
    _validate_tracking_evidence, _verify_continuation_route,
)

from capability_route_dispatch import (
    _expand_parallel_groups, _fallback_chain, _realize_conditional_extensions,
    _validate_dispatch_evidence, _verify_fallback_chain, _workflow_contract,
)

from capability_route_nodes import (
    _expected_nodes_under_frame_policy, _frame_node, _frameless_recipe, _load_sd160_snapshots,
    _no_model_node, _one_leg_frame_recipe, _owner_node, _quick_frame_diversity,
    _quick_gate_bindings, _recipe_has_frame, _resolve_owner_profile, _verify_profile_contract,
    SD160_REGISTRY_DIGEST, SD160_UNIT_CATALOG_DIGEST, unit_catalog_digest,
)

from capability_route_compose import (
    _validate_compose_owner_close, _versioned_subgraph, DECIDED_SHAPES, ROUTE_FRAME_CAPABILITY,
)

from capability_route_scope import (
    _project_entry_scope_nodes, valid_entry_execution_scope_marker,
)


class _ValidationBasisDegrade:
    """Sentinel: an over-ceiling `basis_version` degrades rather than raising."""

_DEGRADE_VALIDATION_BASIS = _ValidationBasisDegrade()

def _check_validation_basis(route, *, allow_stale_registry):
    """Structural/version gate for `validation_basis` (task-brief B-2 §1.3).

    Returns the object when present and well-formed, `None` when the field is
    absent (legacy route), or `_DEGRADE_VALIDATION_BASIS` when the caller
    tolerates staleness and the object's `basis_version` exceeds this
    validator's ceiling. A structurally malformed object always raises on
    either `allow_stale_registry` setting -- no legitimate compiler can emit
    one (every required field is a non-empty absolute-path string by
    construction), so reaching this branch means a hand-edited and resealed
    record.
    """
    if "validation_basis" not in route:
        return None
    basis = route.get("validation_basis")
    if not isinstance(basis, dict):
        raise ValueError("invalid-validation-basis(field=validation_basis)")
    version = basis.get("basis_version")
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise ValueError("invalid-validation-basis(field=basis_version)")
    for field in ("registry_root", "unit_catalog_root", "runtime_root"):
        value = basis.get(field)
        if not isinstance(value, str) or not value or not Path(value).is_absolute():
            raise ValueError(f"invalid-validation-basis(field={field})")
    for field in ("runtime_root_validated", "runtime_root_match"):
        if field in basis and not isinstance(basis[field], bool):
            raise ValueError(f"invalid-validation-basis(field={field})")
    reason = route_format.basis_version_reason(version)
    if reason:
        if allow_stale_registry:
            return _DEGRADE_VALIDATION_BASIS
        raise ValueError(reason)
    return basis

def classify_validation_basis(route, *, registry_digest_now, units_digest_now,
                              registry_root_now, unit_catalog_root_now,
                              capability_digest_now=None):
    """Pure classifier for a route's registry/unit-catalog currentness
    (task-brief B-2 §1.4/§1.5). Never raises and never touches the filesystem.

    `route["validation_basis"]` must already have passed
    `_check_validation_basis`; its absence (legacy route) makes both axes
    classify same-root staleness on a digest mismatch, preserving today's
    exact wording.
    """
    basis = route.get("validation_basis")
    axes = {}
    for axis, own_digest, own_root, digest_key, root_key, stale_message, skew_reason in (
        ("registry", registry_digest_now, registry_root_now,
         "registry_digest", "registry_root", "stale registry digest", "registry-digest-skew"),
        ("unit_catalog", units_digest_now, unit_catalog_root_now,
         "unit_catalog_digest", "unit_catalog_root", "stale unit catalog digest", "unit-catalog-digest-skew"),
    ):
        sealed_digest = route.get(digest_key)
        if sealed_digest is None or sealed_digest == own_digest:
            axes[axis] = {"verdict": "current", "message": None}
            continue
        # The whole registry moved, but the parts this route derives from did
        # not (an edit to another capability): the sealed graph is still valid.
        sealed_capability = route.get("capability_registry_digest")
        if (axis == "registry" and capability_digest_now is not None
                and isinstance(sealed_capability, str) and sealed_capability == capability_digest_now):
            axes[axis] = {"verdict": "current", "message": None}
            continue
        if basis is None or agent_home_equivalent(basis[root_key], own_root):
            axes[axis] = {"verdict": "stale", "message": stale_message}
            continue
        sealed_root = basis[root_key]
        axes[axis] = {
            "verdict": "skew",
            "message": (
                f"{skew_reason}(compiled={sealed_digest}@{sealed_root}, "
                f"validator={own_digest}@{own_root})"
            ),
        }
    verdict, message = "current", None
    for axis in ("registry", "unit_catalog"):
        if axes[axis]["verdict"] != "current":
            verdict, message = axes[axis]["verdict"], axes[axis]["message"]
            break
    return {
        "registry": axes["registry"], "unit_catalog": axes["unit_catalog"],
        "verdict": verdict, "message": message, "basis_present": basis is not None,
    }

def _validate_campaign_selection(key, value):
    pattern = r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}" if key == "campaign_key" else r"cyc_[a-f0-9]{32}"
    if not isinstance(value, str) or re.fullmatch(pattern, value) is None:
        raise ValueError(f"route-{key.replace('_', '-')}-invalid")


def _entry_scope_legacy_registry(registry):
    """Reconstruct only the catalog immediately before the refine preview mark."""
    legacy = json.loads(json.dumps(registry))
    row = ((legacy.get("part_catalog") or {}).get("parts") or {}).get("autopilot-refine:transaction")
    if not isinstance(row, dict) or row.get("start_approval") != "preview":
        return None
    recipe = next((item for item in legacy.get("recipes", [])
                   if item.get("capability") == "autopilot-refine"), None)
    review = next((node for node in ((recipe or {}).get("standard_plus") or {}).get("nodes", [])
                   if node.get("id") == "review"), None)
    if not isinstance(review, dict) or "reviews/refine/preview.md" not in review.get("outputs", []):
        return None
    del row["start_approval"]
    review["outputs"] = [output for output in review["outputs"] if output != "reviews/refine/preview.md"]
    return legacy


def _authenticated_legacy_design_registry(route, basis, current_registry):
    """Reconstruct the exact old design registry from today's flat registry.

    The validated basis is route provenance, not a pointer we need to reopen:
    exact inverse reconstruction plus both route-sealed registry digests prove
    the old declaration. Current units remain independently pinned. This
    keeps the bridge usable after a historical release has been pruned without
    treating an embedded recipe or a basis path as authority.
    """
    if (route.get("capability") != "autopilot-design" or not isinstance(basis, dict)
            or not isinstance(current_registry, dict)):
        return None
    try:
        TOPO.validate_registry(current_registry)
        if (not isinstance(route.get("registry_digest"), str)
                or not isinstance(route.get("capability_registry_digest"), str)
                or not isinstance(route.get("unit_catalog_digest"), str)):
            return None
        if unit_catalog_digest() != route.get("unit_catalog_digest"):
            return None
        # A pruned catalogue is optional; an available contradictory catalogue
        # must not be silently replaced by inverse reconstruction.
        historical_path = Path(basis["registry_root"]) / "capabilities" / "topologies.json"
        try:
            historical = TOPO.load_registry(historical_path)
        except FileNotFoundError:
            pass
        else:
            if TOPO.registry_digest(historical) != route["registry_digest"]:
                return None

        changed = False
        invalid = object()

        def restore_cycle_component(value):
            nonlocal changed
            if isinstance(value, str):
                if "designs/<cycle>" in value:
                    return invalid
                if value == "designs":
                    changed = True
                    return "designs/<cycle>"
                if value.startswith("designs/"):
                    changed = True
                    return "designs/<cycle>/" + value[len("designs/"):]
                return value
            if isinstance(value, list):
                restored = [restore_cycle_component(item) for item in value]
                return invalid if any(item is invalid for item in restored) else restored
            if isinstance(value, dict):
                restored = {}
                for key, item in value.items():
                    restored_value = restore_cycle_component(item)
                    if restored_value is invalid:
                        return invalid
                    restored[key] = restored_value
                return restored
            return value

        sealed = restore_cycle_component(current_registry)
        if not changed or sealed is invalid or not isinstance(sealed, dict):
            return None
        TOPO.validate_registry(sealed)
        design_recipe = next((item for item in sealed.get("recipes", [])
                              if item.get("capability") == "autopilot-design"), None)
        if not isinstance(design_recipe, dict) or "designs/<cycle>" not in json.dumps(
                design_recipe, ensure_ascii=False):
            return None

        if TOPO.registry_digest(sealed) != route.get("registry_digest"):
            # Main added route-local code authoring after these design routes
            # were sealed. Undo only that exact unrelated declaration delta;
            # the complete sealed registry digest must still match.
            codes = [item for item in sealed.get("recipes", [])
                     if item.get("capability") == "autopilot-code"]
            if len(codes) != 1:
                return None
            quick = codes[0].get("quick") or {}
            executes = [item for item in (codes[0].get("standard_plus") or {}).get("nodes", [])
                        if item.get("id") == "execute"]
            if (len(executes) != 1
                    or quick.get("write_scope") != ["source-scoped", "plans/<cycle>/**", "spec/**"]
                    or executes[0].get("write_scope") != [
                        "source/**", "spec/**", "checklist.md", "dev_logs/**", "evidence/**"]
                    or any(item.get("guard_preconditions") != ["artifact-order-prechecked"]
                           for item in (quick, executes[0]))):
                return None
            for item in (quick, executes[0]):
                item["write_scope"].remove("spec/**")
                del item["guard_preconditions"]
            TOPO.validate_registry(sealed)
            if TOPO.registry_digest(sealed) != route.get("registry_digest"):
                return None
        if route.get("composed"):
            recipe = route.get("composed_recipe")
            if (not isinstance(recipe, dict)
                    or not _versioned_subgraph(sealed, recipe)):
                return None
            extras = [recipe]
        else:
            if route.get("composed_recipe") is not None:
                return None
            extras = ()
        if TOPO.capability_registry_digest(sealed, route["capability"], extras) != route.get(
                "capability_registry_digest"):
            return None
        return sealed
    except (OSError, ValueError, TypeError, KeyError):
        return None


def verify_route(route, expected_cwd=None, *, allow_stale_registry=False):
    """Verify a route for mutating/resume use.

    `allow_stale_registry` exists for one caller: closing a route. A registry or unit
    edit inside the same cycle changes the digest and permanently invalidates the route
    compiled before it, so a strict `close` could never record the outcome of exactly
    the work that changed the registry — leaving an open route that `WORKFLOW §0.5`
    calls indistinguishable from abandoned work. Closure writes a sidecar and grants no
    authority, so the route's own integrity (hash, id, cwd) is the right gate there;
    digest currentness stays required for anything that launches, dispatches, or
    mutates, and the closure records which case it was.
    """
    if "work_request" in route:
        from work_start import validate_request
        validate_request(route["work_request"])
    _verify_selection_pins(route)
    if "route_plan" in route:
        # Only the field's own format is verified; the record it names may be gone without
        # making the route unverifiable (`next_leg` simply has nothing to read then).
        import route_plan as RP
        RP.validate_sealed(route["route_plan"])
    reason = route_format.schema_reason(route)
    if reason:
        raise ValueError(reason)
    for key in ("campaign_key", "parent_cycle_id"):
        if key in route:
            _validate_campaign_selection(key, route[key])
    if "slug" in route:
        if not isinstance(route["slug"],str) or not isinstance(route.get("slug_truncated"),bool):
            raise ValueError("invalid route slug metadata")
        try:
            canonical_slug,_=ARTIFACT_LOCATOR.slugify(route["slug"])
        except ARTIFACT_LOCATOR.LocatorError as exc:
            raise ValueError("invalid route slug") from exc
        if canonical_slug != route["slug"]:
            raise ValueError("route slug is not canonical")
    elif "slug_truncated" in route:
        raise ValueError("route slug metadata incomplete")
    reason = route_format.contract_reason(route) or route_format.scope_reason(route)
    if reason:
        raise ValueError(reason)
    if route.get("route_hash") != route_hash(route):
        note_same_work_drift("route-hash-mismatch")
    if route.get("route_id") != "rt-"+route["route_hash"].split(":",1)[1][:16]:
        if not re.fullmatch(r"rt-[0-9a-f]{16}", str(route.get("route_id") or "")):
            raise ValueError("invalid route id")
        note_same_work_drift("route-id-mismatch", str(route.get("route_id")))
    if expected_cwd and Path(expected_cwd).resolve()!=Path(route["cwd"]):
        note_same_work_drift("route-cwd-mismatch", str(expected_cwd))
        route = dict(route, cwd=str(Path(expected_cwd).resolve()))
    _verify_profile_contract(route)
    scope_version = route.get("entry_scope_contract_version")
    entry_scope = route.get("entry_execution_scope")
    if not valid_entry_execution_scope_marker(scope_version, entry_scope):
        raise ValueError("route nodes differ from the declared recipe")
    basis=_check_validation_basis(route, allow_stale_registry=allow_stale_registry)
    if basis is _DEGRADE_VALIDATION_BASIS:
        # An unsupported basis_version is a legitimate newer harness's route;
        # closure records it honestly as unproven rather than stranding it.
        return dict(route, _registry_current=False)
    reason = route_format.persona_reason(route)
    if reason:
        raise ValueError(reason)
    persona_version = route.get("persona_independence_contract_version")
    legacy_unit_view=None
    snapshot_pair=(persona_version is None
                   and route.get("registry_digest")==SD160_REGISTRY_DIGEST
                   and route.get("unit_catalog_digest")==SD160_UNIT_CATALOG_DIGEST)
    snapshots=_load_sd160_snapshots() if snapshot_pair else None
    if snapshots is not None:
        registry, registry_digest_now, current_units_digest, legacy_unit_view = snapshots
    else:
        registry=TOPO.load_registry()
        registry_digest_now=TOPO.registry_digest(registry)
        current_units_digest=unit_catalog_digest()
    capability_digest_now=None
    if isinstance(route.get("capability_registry_digest"), str) and isinstance(route.get("capability"), str):
        extra=[route["composed_recipe"]] if route.get("composed") and isinstance(route.get("composed_recipe"), dict) else ()
        capability_digest_now=TOPO.capability_registry_digest(registry, route["capability"], extra)
    classification=classify_validation_basis(
        route, registry_digest_now=registry_digest_now,
        units_digest_now=current_units_digest,
        registry_root_now=TOPO.ROOT, unit_catalog_root_now=ROOT,
        capability_digest_now=capability_digest_now,
    )
    if (classification["registry"]["verdict"] != "current"
            and classification["unit_catalog"]["verdict"] == "current"):
        legacy_registry = _entry_scope_legacy_registry(registry)
        if legacy_registry is not None and route.get("registry_digest") == TOPO.registry_digest(legacy_registry):
            extra = [route["composed_recipe"]] if route.get("composed") and isinstance(route.get("composed_recipe"), dict) else ()
            legacy_capability_digest = (TOPO.capability_registry_digest(legacy_registry, route["capability"], extra)
                                       if route.get("capability_registry_digest") is not None else None)
            classification = classify_validation_basis(
                route, registry_digest_now=TOPO.registry_digest(legacy_registry),
                units_digest_now=unit_catalog_digest(), registry_root_now=TOPO.ROOT,
                unit_catalog_root_now=ROOT, capability_digest_now=legacy_capability_digest)
            if classification["verdict"] == "current":
                registry = legacy_registry
    if classification["verdict"] != "current":
        sealed_design = _authenticated_legacy_design_registry(route, basis, registry)
        if sealed_design is not None:
            # The original immutable release authenticates the old declaration;
            # the current unit catalog and current launch checks remain active.
            registry = sealed_design
            classification = {
                "registry": {"verdict": "authenticated-sealed-design", "message": None},
                "unit_catalog": {"verdict": "current", "message": None},
                "verdict": "current", "message": None, "basis_present": True,
            }
    if classification["verdict"] != "current":
        if not allow_stale_registry:
            # Catalog drift is expected across releases. Default continuation
            # uses the current tooling without prescribing an unverified old runtime.
            _validate_output_scopes(route.get("nodes", []))
        # A stale/skewed sealed graph cannot be re-derived from the current registry, so
        # every check that compares against it is skipped rather than guessed at.
        return dict(route, _registry_current=False)
    if "continuation_contract_version" in route:
        return _verify_continuation_route(route)
    _validate_output_scopes(route.get("nodes", []))
    def _node_identity(node):
        return {
            k: v for k, v in node.items()
            if k not in ("fallback_hops", "harness_affinity", "harness_policy")
        }
    if route.get("composed"):
        resume_recipe = RESOURCE_RESUME.route_selected(route)
        if route.get("effective_intensity") in ("direct","quick") and not resume_recipe:
            raise ValueError("composed routes require a standard+ effective intensity")
        composed_recipe=route.get("composed_recipe")
        if not isinstance(composed_recipe, dict):
            raise ValueError("composed route lacks embedded composed_recipe")
        with (TOPO._unit_contract_view(legacy_unit_view)
              if legacy_unit_view is not None else contextlib.nullcontext()):
            TOPO._validate_recipe(
                composed_recipe, registry,
                registry["owner_profile_by_intensity"]["standard"],
            )
        _validate_compose_owner_close(registry, composed_recipe, route["effective_intensity"])
        if resume_recipe and not _versioned_subgraph(registry, composed_recipe):
            raise ValueError("resume recipe differs from the declared catalog")
        expected_nodes=(RESOURCE_RESUME.nodes(composed_recipe, registry["owner_profile_by_intensity"]["quick"])
                        if resume_recipe else json.loads(json.dumps(composed_recipe["standard_plus"]["nodes"])))
        expected_nodes=_expand_parallel_groups(
            expected_nodes, composed_recipe["standard_plus"].get("parallel_groups"),
            route.get("effective_intensity"), route.get("capability"),
            auxiliary_check_units=registry.get("auxiliary_check_units"),
            persona_policy=persona_version == 1)
        _project_entry_scope_nodes(expected_nodes, entry_scope)
        if route.get("profile_selection_contract_version") == 1:
            # Same ladder, same order as the compiler: stamp, then seal (under the policy the
            # route's frame nodes were declared with).
            expected_nodes=_expected_nodes_under_frame_policy(
                expected_nodes, route, persona_version=persona_version,
                legacy=_versioned_subgraph(registry, composed_recipe),
                accepts=lambda nodes: ([_node_identity(n) for n in route.get("nodes",[])]
                                       == [_node_identity(n) for n in nodes]))
        if ([_node_identity(n) for n in route.get("nodes",[])]
                != [_node_identity(n) for n in expected_nodes]):
            raise ValueError("composed route nodes differ from embedded composed recipe")
        route_recipe=composed_recipe
    else:
        route_recipe=TOPO.resolve_recipe(
            registry, route.get("capability"), route.get("capability_mode")
        )
        decided=((route.get("selection") or {}).get("route_origin")=="compose"
                 and (route.get("selection") or {}).get("shape") in DECIDED_SHAPES
                 and not any(_frame_node(n) for n in route.get("nodes",[])))
        if (route.get("route_plan") is not None or decided) and route.get("effective_intensity")=="quick":
            # A leg of a plan, or a decided compose shape; a route sealed with its frame pair keeps it.
            route_recipe=_frameless_recipe(route_recipe)
        if (route.get("capability")==ROUTE_FRAME_CAPABILITY
                and [n.get("id") for n in route.get("nodes",[])]==TOPO.ROUTE_FRAME_ONE_LEG_NODE_IDS):
            route_recipe=_one_leg_frame_recipe(route_recipe)
        if route.get("effective_intensity") not in ("direct", "quick"):
            expected_nodes=json.loads(json.dumps(route_recipe["standard_plus"]["nodes"]))
            expected_nodes=_expand_parallel_groups(
                expected_nodes, route_recipe["standard_plus"].get("parallel_groups"),
                route.get("effective_intensity"), route.get("capability"),
                auxiliary_check_units=registry.get("auxiliary_check_units"),
            persona_policy=persona_version == 1)
            _project_entry_scope_nodes(expected_nodes, entry_scope)
            if route.get("profile_selection_contract_version") == 1:
                # Same ladder, same order as the compiler: stamp, then seal (under the policy the
                # route's frame nodes were declared with).
                def declared(nodes):
                    by_id = {n["id"]: n for n in nodes}
                    return not any(
                        (expected := by_id.get(node.get("id"))) and not _no_model_node(node) and any(
                            node.get(key) != expected.get(key)
                            for key in ("profile_demand", "profile_selection", "model_profile"))
                        for node in route.get("nodes", []))
                expected_nodes=_expected_nodes_under_frame_policy(
                    expected_nodes, route, persona_version=persona_version, legacy=True,
                    accepts=declared)
                by_id = {n["id"]: n for n in expected_nodes}
                for node in route.get("nodes", []):
                    expected = by_id.get(node.get("id"))
                    if expected and not _no_model_node(node) and any(
                        node.get(key) != expected.get(key)
                        for key in ("profile_demand", "profile_selection", "model_profile")):
                        raise ValueError("node-profile-declaration-mismatch:" + node["id"])
            # The remaining verifier owns field-level diagnostics.  This
            # census closes only the undeclared fanout hole: a rehashed route
            # may not add, remove, reorder, or rename recipe nodes.
            if ([n.get("id") for n in route.get("nodes", [])]
                    != [n.get("id") for n in expected_nodes]):
                raise ValueError("route nodes differ from the declared recipe")
            if (route.get("capability") == ROUTE_FRAME_CAPABILITY
                    and route.get("profile_selection_contract_version") == 1
                    and [_node_identity(n) for n in route.get("nodes", [])]
                    != [_node_identity(n) for n in expected_nodes]):
                # The framed route's model-less terminal is accepted only as exactly the recipe's node.
                raise ValueError("route-frame nodes differ from the sealed recipe")
    if (route.get("capability") == ROUTE_FRAME_CAPABILITY) != (route.get("selection", {}).get("shape") == "framed"):
        raise ValueError("route-frame-shape-mismatch")
    if route.get("capability") == ROUTE_FRAME_CAPABILITY and (
            route.get("composed") or route.get("effective_intensity") != "standard"):
        raise ValueError("route-frame-shape-mismatch")
    expected_extensions=_realize_conditional_extensions(
        route_recipe, route.get("effective_intensity")
    )
    if route.get("conditional_extensions") != expected_extensions:
        raise ValueError("route conditional extensions differ from the sealed recipe")
    route_node_ids={node.get("id") for node in route.get("nodes", [])}
    if any(not set(row["after"]) <= route_node_ids for row in expected_extensions):
        raise ValueError("route conditional extension anchor is not realized")
    # Mirror of the compiler: quick binds its own frame gate, only `direct`
    # binds nothing. Verifier and compiler must move together or a quick route
    # compiles and then refuses to verify.
    expected_bindings=json.loads(json.dumps(
        _quick_gate_bindings(route_recipe) if route.get("effective_intensity")=="quick"
        else route_recipe["human_gate_bindings"]
        if route.get("effective_intensity")!="direct" else []))
    if scope_version == 1:
        expected_bindings = [row for row in expected_bindings if row.get("gate") != "preview-disposition"]
    if route.get("human_gate_bindings") != expected_bindings:
        raise ValueError("route human gate bindings differ from the sealed recipe")
    if route.get("workflow_contract") != _workflow_contract(
            registry, route.get("nodes",[]), expected_bindings):
        raise ValueError("route workflow contract differs from the realized stage graph")
    if {row["gate"] for row in expected_bindings} - set(route.get("human_gates") or []):
        raise ValueError("route binds an undeclared human gate")
    if (scope_version != 1 and route.get("effective_intensity") != "direct"
            and "preview-disposition" in (route.get("human_gates") or [])):
        preview_raisers = [n for n in route.get("nodes", [])
                           if n.get("continuation") == {"kind": "human-gate", "gate": "preview-disposition"}
                           or "preview-disposition" in n.get("inline_human_gates", [])]
        preview_bindings = [b for b in expected_bindings if b.get("gate") == "preview-disposition"]
        if not preview_raisers or not preview_bindings:
            raise ValueError("preview-approval-boundary-missing")
    if route.get("owner_dispatch_depth") not in {0, 1} or route.get("max_dispatch_depth") not in {0, 1, 2}:
        raise ValueError("invalid qualified dispatch depth")
    if any(key in route for key in ("depth", "owner_depth", "max_depth")):
        raise ValueError("bare route dispatch-depth fields are forbidden")
    allocation = route.get("dispatch_allocation")
    if allocation is not None:
        required = {"strategy", "window", "harness_order"}
        optional = {"usage_gate_used_percent", "depth_affinity", "depth_affinity_weight", "usage_headroom_exponent",
                    "harness_weights", "owner_order"}
        if not isinstance(allocation, dict) or not required <= set(allocation) or set(allocation) - required - optional:
            raise ValueError("invalid dispatch_allocation shape")
        if allocation.get("strategy") not in {
            "config-order", "least-recent-attempts", "capacity-aware", "balanced"
        }:
            raise ValueError("invalid dispatch_allocation strategy")
        window = allocation.get("window")
        if (
            not isinstance(window, int)
            or window < 0
            or (allocation["strategy"] in {"least-recent-attempts", "capacity-aware", "balanced"} and window < 3)
        ):
            raise ValueError("invalid dispatch_allocation window")
        gate = allocation.get("usage_gate_used_percent", 90)
        if "usage_gate_used_percent" in allocation and (
            not isinstance(gate, int) or not 0 <= gate <= 100
        ):
            raise ValueError("invalid dispatch_allocation usage gate")
        affinity = allocation.get("depth_affinity", {})
        if not isinstance(affinity, dict) or not set(affinity) <= {"owner", "worker"}:
            raise ValueError("invalid dispatch_allocation depth affinity")
        if any(value not in DEFAULTS.DISPATCHABLE_HARNESSES for value in affinity.values()):
            raise ValueError("invalid dispatch_allocation depth affinity value")
        if "owner_order" in allocation and not DEFAULTS.valid_owner_order(allocation["owner_order"]):
            raise ValueError("invalid dispatch_allocation owner order")
        weight = allocation.get("depth_affinity_weight", 0.5)
        if isinstance(weight, bool) or not isinstance(weight, (int, float)) or not 0.0 <= weight <= 1.0:
            raise ValueError("invalid dispatch_allocation affinity weight")
        exponent = allocation.get("usage_headroom_exponent", 1)
        if isinstance(exponent, bool) or not isinstance(exponent, int) or not 1 <= exponent <= 4:
            raise ValueError("invalid dispatch_allocation headroom exponent")
        if "harness_weights" in allocation:
            weights = allocation["harness_weights"]
            if not isinstance(weights, dict) or any(
                name not in DEFAULTS.DISPATCHABLE_HARNESSES
                or isinstance(value, bool) or not isinstance(value, (int, float))
                or not 0.0 < value <= 1.0
                for name, value in weights.items()
            ):
                raise ValueError("invalid dispatch_allocation harness weights")
        order = allocation.get("harness_order")
        if (
            not isinstance(order, list)
            or not order
            or len(order) != len(set(order))
            or any(item not in DEFAULTS.DISPATCHABLE_HARNESSES for item in order)
        ):
            raise ValueError("invalid dispatch_allocation harness order")
    observed_dispatch_depths = [route["owner_dispatch_depth"]]
    effective=route.get("effective_intensity")
    selected_owner, _ = _resolve_owner_profile(
        effective, registry, route.get("owner_profile_demand"),
        (route.get("explicit_profiles") or {}).get("__owner__"))
    if route.get("owner_model_profile") != selected_owner:
        raise ValueError("owner_model_profile differs from the portable owner selection")
    # The owner's own node (quick `one-shot`) must carry the profile the route
    # sealed for the owner (the policy check above already admitted it, `top`
    # included); a standard+ recipe's semantic owner node keeps the portable
    # policy profile below.
    owner_profile=route.get("owner_model_profile")
    expected_owner_profile=(
        None if effective=="direct" else registry["owner_profile_by_intensity"].get(effective)
    )
    for node in route.get("nodes",[]):
        if _owner_node(node, effective) and node.get("model_profile")!=owner_profile:
            raise ValueError(f"owner node {node.get('id')} profile differs from owner_model_profile")
    def validate_harness_policy(policy):
        if policy is None:
            return
        if not isinstance(policy, dict):
            raise ValueError("harness_policy must be a mapping or null")
        flattened = []
        for band in DEFAULTS.QUALITY_BANDS:
            values = policy.get(band)
            if not isinstance(values, list) or any(
                value not in DEFAULTS.DISPATCHABLE_HARNESSES for value in values
            ):
                raise ValueError(f"invalid harness_policy band: {band}")
            flattened.extend(values)
        if len(flattened) != len(set(flattened)):
            raise ValueError("harness_policy repeats a harness across bands")
        threshold = policy.get("promote_relief_below")
        if not isinstance(threshold, int) or not 0 <= threshold <= 100:
            raise ValueError("invalid harness_policy promote_relief_below")
    owner_policy = route.get("owner_harness_policy")
    validate_harness_policy(owner_policy)
    if effective == "direct" and owner_policy is not None:
        raise ValueError("direct route cannot carry owner_harness_policy")
    if owner_policy is not None and allocation is not None:
        owner_set = {
            harness for band in DEFAULTS.QUALITY_BANDS for harness in owner_policy[band]
        }
        # A profile's policy may leave a pool harness out (for example OpenCode
        # is not used for deep work); it may never name one outside the pool.
        if not owner_set <= set(allocation["harness_order"]):
            raise ValueError("owner_harness_policy differs from dispatch allocation pool")
    realized_groups = {}
    for node in route.get("nodes", []):
        if node.get("kind") == "resource-runner":
            if any(
                key in node
                for key in (
                    "depth", "owner_depth", "max_depth", "dispatch_depth",
                    "transport", "fallback_hops",
                )
            ):
                raise ValueError(f"resource node {node.get('id')} has dispatch attempt fields")
            if node.get("resource_transport") != "detached-process":
                raise ValueError(f"resource node {node.get('id')} lacks detached lifecycle")
            continue
        if node.get("dispatch_depth") in {1, 2}:
            profile = node.get("model_profile")
            row = registry["model_profiles"].get(profile)
            # The `top` exception profile is unregistered on purpose, so only
            # the two node classes allowed to hold it skip the registered check:
            # the owner, and a depth-1 frame anchor leg.
            top_owner_node = profile == PROFILE.TOP_PROFILE and (
                _owner_node(node, effective) or _frame_node(node)
            )
            if not top_owner_node and (
                not isinstance(row, dict) or row.get("registered_topology") is not True
            ):
                raise ValueError(f"node {node.get('id')} has invalid registered model_profile")
        if (
            effective not in ("direct", "quick")
            and node.get("kind") == "capability-owner"
            and node.get("unit") == "_kernel/owner"
            and (
                node.get("dispatch_depth") != 1
                or (route.get("profile_selection_contract_version") is None
                    and node.get("model_profile") != expected_owner_profile)
            )
        ):
            raise ValueError(
                f"semantic capability owner {node.get('id')} differs from "
                "the portable standard+ owner policy"
            )
        if any(key in node for key in ("depth", "owner_depth", "max_depth")) or node.get("dispatch_depth") not in {0, 1, 2}:
            raise ValueError(f"node {node.get('id')} has invalid dispatch_depth")
        observed_dispatch_depths.append(node["dispatch_depth"])
        if "harness_affinity" in node and node["harness_affinity"] not in VALID_AFFINITY:
            raise ValueError(f"invalid harness_affinity vocabulary: {node['harness_affinity']!r}")
        policy = node.get("harness_policy")
        validate_harness_policy(policy)
        if "execution_surface" in node and node["execution_surface"] not in EXECUTION_SURFACES:
            raise ValueError(f"invalid execution_surface vocabulary: {node['execution_surface']!r}")
        if "registered_worker" in node and not isinstance(node["registered_worker"], bool):
            raise ValueError("registered_worker must be boolean")
        if "dispatch_fallback" in node:
            raise ValueError("legacy dispatch_fallback is read-only")
        for hop in node.get("fallback_hops", []):
            if not isinstance(hop, dict) or hop.get("fallback_hop") not in FALLBACK_HOPS:
                raise ValueError(f"invalid fallback_hop vocabulary: {hop!r}")
        group_id=node.get("parallel_group")
        if group_id:
            if node.get("replica_group") != group_id:
                raise ValueError(f"node {node.get('id')} has inconsistent parallel-group alias")
            realized_groups.setdefault(group_id,[]).append(node)
    expected_group_rows=[]
    for group_id, members in realized_groups.items():
        members.sort(key=lambda member: member.get("parallel_leg_index", -1))
        width=members[0].get("parallel_leg_count") if members else 0
        if width != len(members) or [member.get("parallel_leg_index") for member in members] != list(range(width)):
            raise ValueError(f"parallel group {group_id} has incomplete/duplicate leg indexes")
        invariant_fields=("parallel_group_kind","parallel_join_policy","parallel_independence_axes")
        if any(member.get("parallel_leg_count") != width for member in members):
            raise ValueError(f"parallel group {group_id} width metadata mismatch")
        if any(member.get(field) != members[0].get(field) for member in members for field in invariant_fields):
            raise ValueError(f"parallel group {group_id} invariant metadata mismatch")
        for index,left in enumerate(members):
            for right in members[index+1:]:
                if any(TOPO._overlap(a,b) for a in left.get("write_scope",[]) for b in right.get("write_scope",[])):
                    raise ValueError(f"parallel group {group_id} has overlapping write scopes")
        expected_group_rows.append({
            "id":group_id,
            "kind":members[0]["parallel_group_kind"],
            "join_policy":members[0]["parallel_join_policy"],
            "independence_axes":members[0]["parallel_independence_axes"],
            "width":width,
            "members":[member["id"] for member in members],
        })
    if route.get("parallel_groups") != expected_group_rows:
        raise ValueError("route parallel_groups summary differs from realized nodes")
    if route["max_dispatch_depth"] != max(observed_dispatch_depths):
        raise ValueError("max_dispatch_depth does not match the realized route")
    selection=route.get("selection",{})
    if effective=="direct":
        if (
            route.get("owner_dispatch_depth") != 0
            or route.get("max_dispatch_depth") != 0
            or selection.get("transport") != "interactive"
            or route.get("registered_headless_candidates") is not None
            or route.get("registered_headless_policy") is not None
            or len(route.get("nodes",[])) != 1
        ):
            raise ValueError("direct route shape mismatch")
        node=route["nodes"][0]
        if (
            node.get("id") != "inline"
            or node.get("dispatch_depth") != 0
            or node.get("execution_surface") != "inline"
            or node.get("registered_worker") is not False
            or node.get("fallback_hops")
        ):
            raise ValueError("direct node axes mismatch")
    elif effective=="quick":
        resume_recipe = RESOURCE_RESUME.route_selected(route)
        if (
            route.get("owner_dispatch_depth") != 1
            or route.get("max_dispatch_depth") != 1
            or selection.get("transport") != "headless"
            or selection.get("inline_reason") is not None
            # `serial-attempt` is a per-(route_id, route_node) attempt budget,
            # so it stays true unchanged with three nodes. `max_dispatch_depth`
            # stays 1 because the frame legs are depth 1 as well.
            or route.get("registered_headless_policy") != "serial-attempt"
            or len(route.get("nodes",[])) != (2 if resume_recipe else 3 if _recipe_has_frame(route_recipe) else 1)
        ):
            raise ValueError("quick route shape mismatch")
        candidates=_validate_registered_headless_evidence({
            "candidates":route.get("registered_headless_candidates")
        })
        if candidates != route.get("registered_headless_candidates"):
            raise ValueError("quick registered-headless evidence is not canonical")
        diversity=_quick_frame_diversity(candidates)
        if any(n.get("harness_diversity") != diversity
               for n in route.get("nodes",[]) if _frame_node(n)):
            raise ValueError("quick route frame harness diversity mismatch")
        node=next((n for n in route["nodes"] if n.get("id")=="one-shot"), None)
        if node is None:
            raise ValueError("quick route shape mismatch")
        expected_inline_gates = list(route_recipe["quick"].get("inline_human_gates", []))
        if scope_version == 1:
            expected_inline_gates = [gate for gate in expected_inline_gates if gate != "preview-disposition"]
        if node.get("inline_human_gates", []) != expected_inline_gates:
            raise ValueError("quick-inline-human-gates-mismatch")
        expected_scope = (route_recipe["standard_plus"]["nodes"][1]["write_scope"] if resume_recipe
                          else route_recipe["quick"]["write_scope"])
        if node.get("write_scope") != expected_scope:
            raise ValueError("quick-write-scope-mismatch")
        if (
            node.get("dispatch_depth") != 1
            or node.get("unit") != "_kernel/owner"
            or node.get("model_profile") != owner_profile
            or node.get("execution_surface") != "registered-headless"
            or node.get("registered_worker") is not True
            or node.get("fallback_hops")
            or sorted(node.get("depends_on") or []) !=
               (["resume-run"] if resume_recipe else ["frame","frame-alternative"] if _recipe_has_frame(route_recipe) else [])
        ):
            raise ValueError("quick node axes mismatch")
        frame_legs=[n for n in route["nodes"] if _frame_node(n)]
        if sorted(n.get("id") for n in frame_legs) != (
                ["frame","frame-alternative"] if _recipe_has_frame(route_recipe) else []):
            raise ValueError("quick route frame pair mismatch")
        for leg in frame_legs:
            if (
                leg.get("execution_surface") != "registered-headless"
                or leg.get("registered_worker") is not True
                or leg.get("fallback_hops")
                or leg.get("launch_authority") != "depth-0"
            ):
                raise ValueError("quick frame leg axes mismatch")
    dd_digest=route.get("dispatch_defaults_digest")
    if dd_digest is not None and (not isinstance(dd_digest, str) or not dd_digest.startswith("sha256:")):
        raise ValueError("invalid dispatch_defaults_digest format")
    _validate_tracking_evidence(route.get("tracking"), route.get("tracked_gate_evidence"))
    escalation=route.get("selection",{}).get("escalation_basis")
    if not isinstance(escalation,list): raise ValueError("escalation_basis missing")
    if any(row.get("signal") in TRACKING for row in escalation if isinstance(row,dict)):
        raise ValueError("tracking cannot be an escalation basis")
    spec_touch=any(_scope_touches_spec(scope) for node in route.get("nodes",[]) for scope in node.get("write_scope",[]))
    if bool(route.get("spec_touch")) != spec_touch: raise ValueError("spec_touch declaration mismatch")
    if effective not in ("direct","quick") and _single_owner_nodes(route.get("nodes",[])):
        candidates = _validate_registered_headless_evidence({"candidates": route.get("registered_headless_candidates")})
        if (candidates != route.get("registered_headless_candidates")
                or route.get("registered_headless_policy") is not None
                or route.get("dispatch_evidence") is not None
                or route["nodes"][0].get("fallback_hops")
                or route.get("selection",{}).get("transport") != "headless"):
            raise ValueError("owner-only registered-headless evidence mismatch")
    elif route.get("effective_intensity") not in ("direct","quick"):
        if route.get("registered_headless_candidates") is not None:
            raise ValueError("nested route cannot substitute owner-only readiness")
        if route.get("selection",{}).get("transport") != "headless":
            raise ValueError("standard+ routes require checked headless transport")
        contract_version=route.get("dispatch_contract_version") or route.get("broker_contract_version") or 1
        parent_dispatch_depth=_evidence_parent_dispatch_depth(
            route.get("nodes",[]), route.get("owner_dispatch_depth"))
        checked_dispatch=_validate_dispatch_evidence(
            route.get("dispatch_evidence"), contract_version, parent_dispatch_depth,
            expected_worktree=route.get("cwd"),
            require_scope=scope_version == DISPATCH_EVIDENCE_SCOPE_VERSION)
        expected_chain=_fallback_chain(
            checked_dispatch, contract_version, parent_dispatch_depth,
            expected_worktree=route.get("cwd"),
            require_scope=scope_version == DISPATCH_EVIDENCE_SCOPE_VERSION)
        for node in route.get("nodes",[]):
            if node.get("dispatch_depth")==2:
                chain=_verify_fallback_chain(node, contract_version)
                if chain != expected_chain:
                    raise ValueError(f"dispatch-depth-2 node {node.get('id')} fallback differs from checked evidence")
    return route
