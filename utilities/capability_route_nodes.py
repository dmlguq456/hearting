"""Capability route: owner, frame and no-model nodes, unit snapshots, and sealed modes.

Split out of `capability-route.py`, which re-exports every name defined here.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path

import dispatch_runtime_support as RUNTIME_SUPPORT
import model_profile as PROFILE

from capability_route_base import (
    DEFAULTS, ROOT, TOPO,
)


def _resolve_owner_profile(effective, registry, demand=None, explicit_profile=None):
    """One selection for compile and verify: demand, otherwise legacy default.

    Intensity describes the workflow shape; its compatibility default must not
    veto the independently validated judgment/execution demand.
    """
    default = registry["owner_profile_by_intensity"].get(effective) or "light"
    selection = PROFILE.resolve_profile_demand(
        demand, explicit_profile=explicit_profile if explicit_profile is not None else default if demand is None else None,
        legacy=explicit_profile is None, existing_versioned_stage=True)
    profile = selection["resolved_profile"]
    if effective == "direct":
        if profile == PROFILE.TOP_PROFILE:
            raise ValueError("owner-profile-top-requires-owner")
        return None, selection
    if profile != PROFILE.TOP_PROFILE and registry["model_profiles"].get(
            profile, {}).get("registered_topology") is not True:
        eligible = "|".join(name for name, row in registry["model_profiles"].items()
                            if row.get("registered_topology") is True)
        raise ValueError(f"owner-profile-ineligible-for-registered-dispatch: {profile} cannot run a registered "
                         f"owner; use --profile {eligible}, or --shape direct to work in this session")
    return profile, selection


def _owner_node(node, effective):
    """Whether `node` IS the route's owner: only the quick shape's `one-shot`
    node (the owner and the node are one process there). A standard+ recipe's
    depth-1 `_kernel/owner` *stage* nodes (prd-transaction, handback, deploy,
    ...) are not the owner and never inherit its profile (review R2 B1: sealing
    `top` onto them made compile and verify disagree again, and would have
    spent the main-session model on stages nobody asked for). One predicate
    for the compiler and every verify check."""

    return (effective == "quick" and node.get("id") == "one-shot"
            and node.get("dispatch_depth") == 1 and node.get("unit") == "_kernel/owner")


def _no_model_node(node):
    """Whether `node` runs no model: a detached resource run, or the framed route's runtime terminal."""
    return node.get("kind") in ("resource-runner", TOPO.ROUTE_DECISION_KIND)


def _frame_node(node):
    """Whether `node` is a frame bootstrap leg: the depth-1 direction-setting
    pair that the depth-0 session launches itself, ahead of any owner. Mirrors
    `_owner_node`'s style so both the compiler and every verify check read one
    predicate. This is the SECOND node class allowed the `top` exception
    profile -- a frame leg is the one place where spending the main-session
    model buys the whole route its framing, and it is bounded to one leg by
    `replica_batch_contract`'s per-group top cap."""

    return (node.get("unit") == "plan/frame"
            and node.get("dispatch_depth") == 1
            and node.get("worker_type") == "frame")


def _quick_frame_diversity(candidates):
    """ONE definition of a quick frame pair's harness diversity, shared by the
    compiler and `verify_route` (they used to count supported harnesses
    separately, and a policy change on one side alone would have made every
    sealed single-harness route unverifiable).

    Zero supported harnesses cannot frame at all. The supported harness set
    is descriptive provenance, not an independence requirement (SD-160).
    One harness with separate executions and two personas is normal."""

    harnesses = sorted({
        row.get("harness") for row in candidates or []
        if row.get("status") == "supported" and row.get("harness")
    })
    if not harnesses:
        raise ValueError("quick-frame-harness-unavailable")
    return ("cross-harness" if len(harnesses) >= 2
            else "single-harness:" + harnesses[0])


def _stamp_frame_profiles(nodes, owner_profile, owner_demand, *, seal_persona=True, prior=False):
    """Stamp every frame leg's `model_profile` from the one tier ladder.

    ONE function called by BOTH the compiler and `verify_route`'s expected-node
    recomputation. It has to be shared: the verifier rebuilds the node list
    from the recipe and compares field by field, so a ladder applied on only
    one side reports every standard+ route as
    `node-profile-declaration-mismatch:frame` -- which is exactly what happened
    the first time this was written inline in the compiler.

    Runs BEFORE `_seal_profile_demands` on both sides, because that is what
    turns the stamped profile into the node's sealed selection.

    `prior` is for the verifier alone (`_expected_nodes_under_frame_policy`): it rebuilds the
    declaration an already sealed route made under the one prior frame policy. Compilation never
    passes it."""

    rungs = PROFILE.frame_profile_for_owner(owner_profile, prior=prior)
    if sum(1 for node in nodes if _frame_node(node)) == 1 and not prior:
        # A framed route's one leg (only a framed route has a single frame leg).
        rungs = {"anchor": PROFILE.FRAME_SINGLE_LEG_PROFILE, "others": PROFILE.FRAME_SINGLE_LEG_PROFILE}
    for node in nodes:
        if not _frame_node(node):
            continue
        # SD-160: seal the distinct personas; old routes retain their original
        # bytes and admission resolves the same canonical frame roles.
        if seal_persona:
            node.setdefault("perspective", "primary-frame" if node.get("id") == "frame"
                            else "alternative-frame")
        # Both frame perspectives get the same top-tier default. Explicit
        # route selections are applied later and retain precedence.
        profile = rungs["anchor" if node.get("id") == "frame" else "others"]
        node["model_profile"] = profile
        if profile == PROFILE.TOP_PROFILE:
            # `top` is not a portable profile, so it cannot be sealed through
            # the legacy "explicit profile, no demand" path -- the resolver
            # refuses that with `profile-demand-required`. Give the anchor a
            # real explicit selection instead: the owner's own demand when the
            # caller supplied one, otherwise the frame shape's intrinsic
            # demand. Each leg seals its own selection and demand.
            node["profile_explicit"] = True
            node["profile_demand"] = json.loads(json.dumps(
                owner_demand or PROFILE.frame_anchor_shape_demand(prior=prior)))
    return nodes


def _expected_nodes_under_frame_policy(base_nodes, route, *, persona_version, legacy, accepts):
    """The route's expected nodes, stamped and sealed under ONE frame policy.

    A route sealed before the top/top default keeps the frame declaration it was made with. The
    current policy is tried first; if the route does not hold it, the one prior policy is tried, and
    only as a whole -- every frame node of the route is rebuilt under the same policy, so a route
    that mixes the two is refused. Everything else (graph, scope, gates, explicit caller profiles,
    the demand digests) is rebuilt and compared exactly as before by the caller. When neither policy
    is held, the current policy's nodes (or its error) are returned so the caller reports the
    same diagnostic as before."""

    attempts = (False, True) if any(_frame_node(n) for n in base_nodes) else (False,)
    first_error = first_nodes = None
    for prior in attempts:
        nodes = json.loads(json.dumps(base_nodes))
        try:
            _stamp_frame_profiles(nodes, route.get("owner_model_profile"),
                                  route.get("owner_profile_demand"),
                                  seal_persona=persona_version == 1, prior=prior)
            _seal_profile_demands(nodes, route.get("profile_demands"),
                                  route.get("explicit_profiles"), legacy=legacy)
        except ValueError as exc:
            if not prior:
                first_error = exc
            continue
        if accepts(nodes):
            return nodes
        if not prior:
            first_nodes = nodes
    if first_error is not None:
        raise first_error
    return first_nodes


def _recipe_has_frame(recipe):
    return any(_frame_node(node) for node in recipe["standard_plus"]["nodes"])


def _quick_gate_bindings(recipe):
    """Quick's single human gate binding: the frame pair fences `one-shot`."""

    bindings = ([{"gate": "frame-review", "node": "one-shot", "position": "entry"}]
                if _recipe_has_frame(recipe) else [])
    bindings.extend({"gate": gate, "node": "one-shot", "position": "terminal"}
                    for gate in recipe["quick"].get("inline_human_gates", []))
    return bindings


def _one_leg_frame_recipe(recipe):
    """The framed recipe that runs one frame leg: the alternative leg is gone and the decision
    terminal depends on `frame` alone. ONE projection, used by the compiler and `verify_route`."""
    view=json.loads(json.dumps(recipe))
    nodes=[n for n in view["standard_plus"]["nodes"] if n.get("id")!="frame-alternative"]
    for node in nodes:
        if node.get("id")=="route-decision":
            node["depends_on"]=["frame"]
    view["standard_plus"]["nodes"]=nodes
    return view


def _frameless_recipe(recipe):
    """The recipe as a leg of an approved route plan sees it: the frame pair, its gate and its
    binding are gone, exactly as for a capability that never had a frame layer."""
    view=json.loads(json.dumps(recipe))
    view["standard_plus"]["nodes"]=[n for n in view["standard_plus"]["nodes"] if not _frame_node(n)]
    view["human_gates"]=[g for g in view["human_gates"] if g!="frame-review"]
    view["human_gate_bindings"]=[b for b in view["human_gate_bindings"] if b.get("gate")!="frame-review"]
    return view


def _seal_profile_demands(nodes, profile_demands=None, explicit_profiles=None, *, legacy=False):
    demands = profile_demands or {}
    explicit_profiles = explicit_profiles or {}
    for node in nodes:
        if _no_model_node(node):
            continue
        node_id = node["id"]
        demand = demands.get(node_id, node.get("profile_demand"))
        supplied = demand is not None
        if supplied:
            demand = PROFILE.normalize_profile_demand(demand)
        explicit = explicit_profiles.get(node_id)
        if node.get("profile_explicit") and node_id not in explicit_profiles:
            explicit = node.get("model_profile")
        if not supplied:
            if explicit is None and legacy:
                explicit = node.get("model_profile", "light")
        selection = PROFILE.resolve_profile_demand(
            demand, explicit_profile=explicit, legacy=legacy and node_id not in explicit_profiles and not node.get("profile_explicit"),
            existing_versioned_stage=legacy,
        )
        node["profile_demand"] = demand
        node["profile_selection"] = selection
        node["model_profile"] = selection["resolved_profile"]
    return nodes


def _profile_input_maps(nodes, demands, explicit):
    valid = {n["id"] for n in nodes if not _no_model_node(n)} | {"__owner__"}
    normalized = {}
    for label, values in (("profile_demands", demands), ("explicit_profiles", explicit)):
        if values is not None and (not isinstance(values, dict) or set(values) - valid):
            raise ValueError("profile-input-unknown-node:" + label)
    for key, value in (demands or {}).items():
        normalized[key] = PROFILE.normalize_profile_demand(value)
    frame_ids = {n["id"] for n in nodes if _frame_node(n)}
    for key, value in (explicit or {}).items():
        if value not in PROFILE.KNOWN_PROFILES:
            raise ValueError("profile-explicit-input-invalid:" + key)
        if value == PROFILE.TOP_PROFILE and key != "__owner__" and key not in frame_ids:
            # The top exception profile is a dispatch-depth-1 decision: a
            # depth-2 stage node or parallel leg never spends the main-session
            # model. A depth-1 frame leg is the one added exception -- it is an
            # anchor for the whole route's direction, launched by the depth-0
            # session itself.
            raise ValueError("profile-explicit-top-owner-only:" + key)
    return normalized, dict(explicit or {})


def _verify_profile_contract(route):
    version = route.get("profile_selection_contract_version")
    if version is None:
        # Exact sealed legacy routes contain no new semantic fields.
        if route.get("owner_profile_selection") is not None or any(
            "profile_selection" in n or "profile_demand" in n for n in route.get("nodes", [])):
            raise ValueError("profile-selection-contract-missing")
        return
    if type(version) is not int or version != 1:
        raise ValueError("profile-selection-version-unsupported")
    demands, explicit = _profile_input_maps(route.get("nodes", []), route.get("profile_demands"), route.get("explicit_profiles"))
    if route.get("owner_profile_demand") != demands.get("__owner__"):
        raise ValueError("owner-profile-demand-map-mismatch")
    owner_profile = route.get("owner_model_profile") or "light"
    owner_expected = PROFILE.resolve_profile_demand(
        demands.get("__owner__"), explicit_profile=(explicit.get("__owner__")
            if "__owner__" in explicit or "__owner__" in demands else owner_profile),
        legacy="__owner__" not in explicit, existing_versioned_stage=True)
    if owner_expected != route.get("owner_profile_selection"):
        raise ValueError("owner-profile-selection-map-mismatch")
    for node in route.get("nodes", []):
        node_id = node["id"]
        if node_id in demands and node.get("profile_demand") != demands[node_id]:
            raise ValueError("node-profile-demand-map-mismatch:" + node_id)
        if node_id in explicit and (node.get("profile_selection", {}).get("source") != "explicit"
                                   or node.get("model_profile") != explicit[node_id]):
            raise ValueError("node-profile-explicit-map-mismatch:" + node_id)
    PROFILE.validate_profile_selection(
        route.get("owner_profile_selection"), route.get("owner_profile_demand"),
        profile=route.get("owner_model_profile"), existing_versioned_stage=True,
    )
    for node in route.get("nodes", []):
        if _no_model_node(node):
            continue
        PROFILE.validate_profile_selection(node.get("profile_selection"), node.get("profile_demand"),
                                           profile=node.get("model_profile"), existing_versioned_stage=True)


def _seal_confirmation_mode():
    """SD-123: resolve the declared `confirmation.mode` to seal into the
    route, independent of `_seal_dispatch_defaults`'s return tuple.

    T-3: `_seal_dispatch_defaults` returns `(None, None, None)` early when
    the user config file is absent, and threading `confirmation_mode`
    through that tuple would seal `None` for exactly that user instead of
    the `hybrid` default -- the "configured but not applied" drift class.
    This helper is deliberately separate and always returns a real mode.
    """
    config_path = DEFAULTS.default_config_path()
    if not os.path.exists(config_path):
        return DEFAULTS.DEFAULT_CONFIRMATION_MODE
    try:
        cfg = DEFAULTS.load_and_validate(config_path, DEFAULTS.default_topology_path())
    except (DEFAULTS.DefaultsConfigError, OSError, json.JSONDecodeError):
        return DEFAULTS.DEFAULT_CONFIRMATION_MODE
    return DEFAULTS.query_confirmation_mode(cfg)

def _seal_small_work_confirmation():
    """SD-136: seal `confirmation.small_work` (notice|card) the same way as
    `_seal_confirmation_mode`: absent/corrupt config -> the shipped default,
    never None."""
    config_path = DEFAULTS.default_config_path()
    if not os.path.exists(config_path):
        return DEFAULTS.DEFAULT_SMALL_WORK_CONFIRMATION
    try:
        cfg = DEFAULTS.load_and_validate(config_path, DEFAULTS.default_topology_path())
    except (DEFAULTS.DefaultsConfigError, OSError, json.JSONDecodeError):
        return DEFAULTS.DEFAULT_SMALL_WORK_CONFIRMATION
    return DEFAULTS.query_small_work_confirmation(cfg)


def _seal_terminal_commit_support(validation_basis):
    """PRD §13.53.2: seal SD-120/121 activation as *checked support*.

    Activation is never an owner-remembered switch. The route declares support
    only when the runtime root it is bound to actually publishes the whole
    contract -- producer binding, terminal transaction, claim fence, exact
    finalize, the supervisor gate, and the registered lock-order table -- and
    the operator has not disabled it. Hash agreement alone can never open it,
    which is exactly the D-2 regression §13.36.6 rejected.

    Fail-closed: any unreadable root, missing surface, unparsable config or
    unexpected error yields `False`. The Claude adapter remains the only
    consumer of the flag, so declaring support claims nothing about Codex or
    OpenCode parity (§13.36.4).
    """
    try:
        config_path = DEFAULTS.default_config_path()
        if os.path.exists(config_path):
            try:
                config = DEFAULTS.load_and_validate(config_path, DEFAULTS.default_topology_path())
            except (DEFAULTS.DefaultsConfigError, OSError, json.JSONDecodeError):
                # A config that exists but does not validate is *not* the same
                # as no config. Falling back to `None` here would discard an
                # operator's `off` whenever some unrelated key in the same file
                # was stale or misspelled -- the switch would be ignored exactly
                # when the file is damaged, which is when it is most likely to
                # have been reached for. Refuse instead.
                return False
        else:
            config = None
        verdict = RUNTIME_SUPPORT.terminal_commit_support(
            (validation_basis or {}).get("runtime_root"), config)
        return bool(verdict.get("supported"))
    except Exception:
        return False

def unit_catalog_digest(units_root=None):
    """Digest of unit frontmatter blocks (machine contracts); unit BODY prose stays un-hashed."""
    return _unit_blocks_digest(_capture_unit_frontmatter(units_root))


def _capture_unit_frontmatter(units_root=None):
    """Capture the exact hashed blocks, retaining malformed files as None."""
    units_root=Path(units_root) if units_root else ROOT/"roles"/"units"
    captured={}
    for path in sorted(units_root.glob("*/*.md")):
        if path.name.startswith("_"): continue
        captured[path.relative_to(units_root).as_posix()] = _unit_frontmatter_block(path)
    return captured


# Each route verification digests every unit's frontmatter; the readiness
# thread verifies routes every second. An unchanged unit file (same inode,
# size, mtime and ctime) is not read and matched again.
_UNIT_BLOCKS = {}


def _unit_frontmatter_block(path):
    info=path.stat()
    generation=(info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns,info.st_ctime_ns)
    key=str(path)
    held=_UNIT_BLOCKS.get(key)
    if held is not None and held[0]==generation:
        return held[1]
    match=re.match(r"\A---\n.*?\n---\n", path.read_text(encoding="utf-8"), re.DOTALL)
    block=match.group(0) if match else None
    if len(_UNIT_BLOCKS) > 4096:
        _UNIT_BLOCKS.clear()
    _UNIT_BLOCKS[key]=(generation,block)
    return block


def _unit_blocks_digest(captured):
    blocks=[f"{path}\n{block}" for path,block in sorted(captured.items()) if block is not None]
    return "sha256:"+hashlib.sha256("\n".join(blocks).encode()).hexdigest()


SD160_REGISTRY_DIGEST = "sha256:4260c26aebc1ab2f5bbb722d1b77a0d3fe555504a4122ec5dd3c891dd8071f10"
SD160_UNIT_CATALOG_DIGEST = "sha256:a565c51b6d07e301836027295e3e49d01dcb2b2bdacb98845441711c73188314"
SD160_SNAPSHOT_DIR = ROOT / "utilities" / "legacy-route-snapshots"


def _sd160_snapshot_path(digest):
    """Map a sealed sha256 digest to its Windows-safe snapshot filename."""
    if not isinstance(digest, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise ValueError("invalid SD-160 snapshot digest")
    return SD160_SNAPSHOT_DIR / f"sha256-{digest.split(':', 1)[1]}.json"


def _load_sd160_snapshots():
    """Load the immutable registry and parsed unit contracts for sealed SD-160 routes."""
    try:
        registry_path = _sd160_snapshot_path(SD160_REGISTRY_DIGEST)
        units_path = _sd160_snapshot_path(SD160_UNIT_CATALOG_DIGEST)
        registry = json.loads(registry_path.read_text(encoding="utf-8"))
        blocks = json.loads(units_path.read_text(encoding="utf-8"))
        registry_digest = TOPO.registry_digest(registry)
        units_digest = _unit_blocks_digest(blocks)
        registry_filename_digest = "sha256:" + registry_path.stem.removeprefix("sha256-")
        units_filename_digest = "sha256:" + units_path.stem.removeprefix("sha256-")
        if registry_digest != registry_filename_digest or units_digest != units_filename_digest:
            return None
        if registry_digest != SD160_REGISTRY_DIGEST or units_digest != SD160_UNIT_CATALOG_DIGEST:
            return None
        fields = {}
        for path, block in blocks.items():
            unit = path[:-3]
            fields[unit] = TOPO._parse_unit_frontmatter(unit, block)
        return registry, registry_digest, units_digest, fields
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
        return None
