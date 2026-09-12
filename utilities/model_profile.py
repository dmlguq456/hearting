#!/usr/bin/env python3
"""Resolve route-sealed portable execution profiles through adapter config."""

from __future__ import annotations

import re
import hashlib
import json
from pathlib import Path
from typing import Mapping


PORTABLE_PROFILES = ("deep", "balanced-deep", "balanced", "light", "mini")
# The exception profile above `deep` (2026-09-09 사용자 결정): each harness's top
# model -- the one CFG_MAIN_SESSION_ONLY_MODELS reserves for the session the
# user talks to -- reached only through a route that sealed an explicit `top`
# selection, with a full demand, for its dispatch-depth-1 owner (see
# `TOP_WORKER_TYPES` and `require_top_route`). It is not portable in the
# five-profile sense: no matrix cell resolves to it, no policy band names it,
# no capacity cascade enters or leaves it, and a runtime config that does not
# declare CFG_MODEL_PROFILE_TOP refuses it typed instead of deriving a model.
TOP_PROFILE = "top"
EXCEPTION_PROFILES = (TOP_PROFILE,)
KNOWN_PROFILES = PORTABLE_PROFILES + EXCEPTION_PROFILES
# The two worker types a route may seal `top` for.
#
# `owner` is the original: the one worker a route seals a profile for.
#
# `frame` is the second, added by the frame bootstrap layer. Note that the
# older rationale here -- "a review worker has no route, so it cannot carry the
# judgment record the exception requires" -- is NOT why frame qualifies: a
# depth-1 frame leg is a registered node of a compiled route and carries route
# evidence exactly like an owner does, so that argument neither admits nor
# excludes it. The real reason is semantic: the frame pair is the one non-owner
# node class whose *direction* genuinely benefits from the strongest available
# reasoning. It runs once, before any owner exists, and every later node of the
# route inherits the framing it produces -- a bad frame is not a bad stage, it
# is a route pointed at the wrong problem. Bounded on both sides: only the
# anchor leg of the pair reaches `top` (`frame_profile_for_owner`), and
# `replica_batch_contract.MAX_TOP_LEGS` caps a group at one `top` leg.
TOP_WORKER_TYPES = frozenset({"owner", "frame"})
RESOLVER_VERSION = "profile-demand/v1"
DEMAND_SCHEMA_VERSION = 1
DEMAND_JUDGMENTS = ("predetermined", "important", "difficult-uncertain")
DEMAND_SCOPES = ("short-local", "extended-multistep")
DEMAND_FIELDS = frozenset({
    "schema_version", "judgment_requirement", "execution_scope",
    "judgment_reason", "execution_reason", "evidence_refs",
})
JUDGMENT_FLOORS = {
    "predetermined": ("light", "balanced"),
    "important": ("balanced-deep", "deep"),
    "difficult-uncertain": ("deep",),
}
MATRIX = {
    ("predetermined", "short-local"): "light",
    ("predetermined", "extended-multistep"): "balanced",
    ("important", "short-local"): "balanced-deep",
    ("important", "extended-multistep"): "balanced-deep",
    ("difficult-uncertain", "short-local"): "deep",
    ("difficult-uncertain", "extended-multistep"): "deep",
}
SUBSTANTIVE_WORKER_TYPES = frozenset({"owner", "stage", "review"})
SAFE_VALUE = re.compile(r"^[A-Za-z0-9._:/ |,-]+$")
SAFE_KEY = re.compile(r"^CFG_[A-Z0-9_]+$")


class ModelProfileError(ValueError):
    def __init__(self, message: str, reason: str = "invalid-profile-demand"):
        super().__init__(message)
        self.reason = reason


def _digest(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def normalize_profile_demand(demand: Mapping[str, object]) -> dict[str, object]:
    """Validate and canonicalize the vendor-neutral two-axis demand object."""
    if not isinstance(demand, Mapping):
        raise ModelProfileError("profile_demand must be an object", "profile-demand-invalid")
    if set(demand) != DEMAND_FIELDS:
        raise ModelProfileError("profile_demand must contain exactly the schema v1 fields",
                                "profile-demand-partial")
    if type(demand.get("schema_version")) is not int or demand["schema_version"] != DEMAND_SCHEMA_VERSION:
        raise ModelProfileError("unsupported profile_demand schema_version",
                                "profile-demand-version-unsupported")
    judgment = demand.get("judgment_requirement")
    scope = demand.get("execution_scope")
    if judgment not in DEMAND_JUDGMENTS or scope not in DEMAND_SCOPES:
        raise ModelProfileError("unknown profile demand axis", "profile-demand-unknown-axis")
    reasons = (demand.get("judgment_reason"), demand.get("execution_reason"))
    if any(not isinstance(value, str) or not value.strip() for value in reasons):
        raise ModelProfileError("profile demand reasons must be non-empty strings",
                                "profile-demand-empty-reason")
    evidence = demand.get("evidence_refs")
    if (not isinstance(evidence, list) or not evidence or
            any(not isinstance(value, str) or not value.strip() for value in evidence)):
        raise ModelProfileError("profile demand evidence_refs must be non-empty strings",
                                "profile-demand-empty-evidence")
    return {
        "schema_version": DEMAND_SCHEMA_VERSION,
        "judgment_requirement": judgment,
        "execution_scope": scope,
        "judgment_reason": reasons[0].strip(),
        "execution_reason": reasons[1].strip(),
        "evidence_refs": [value.strip() for value in evidence],
    }


def resolve_profile_demand(
    demand: Mapping[str, object] | None,
    *,
    explicit_profile: str | None = None,
    legacy: bool = False,
    existing_versioned_stage: bool = False,
) -> dict[str, object]:
    """Resolve one demand into a sealed selection; no adapter/model knowledge."""
    if demand is None:
        if legacy and existing_versioned_stage and explicit_profile in PORTABLE_PROFILES:
            return {
                "schema_version": 1, "source": "legacy", "resolver_version": RESOLVER_VERSION,
                "demand_digest": None, "resolved_profile": explicit_profile, "judgment_floor": "unknown",
                "reason": "unannotated-existing-stage",
            }
        if explicit_profile in KNOWN_PROFILES:
            return {
                "schema_version": 1, "source": "explicit", "resolver_version": RESOLVER_VERSION,
                "demand_digest": None, "resolved_profile": explicit_profile,
                "judgment_floor": "unknown", "reason": "explicit-profile-choice",
            }
        raise ModelProfileError("new or ad-hoc stages require full profile_demand",
                                "profile-demand-required")
    normalized = normalize_profile_demand(demand)
    judgment = normalized["judgment_requirement"]
    scope = normalized["execution_scope"]
    matrix_profile = MATRIX[(judgment, scope)]
    if explicit_profile is None:
        resolved = matrix_profile
        source = "matrix"
        reason = "matrix-cell"
    else:
        if explicit_profile not in KNOWN_PROFILES:
            raise ModelProfileError("unknown explicit profile", "profile-explicit-unknown")
        resolved = explicit_profile
        source = "explicit"
        # The matrix recommends a budget; a caller's explicit choice owns it.
        # Retain the old receipts for choices the former recommendation admitted.
        recommended = (explicit_profile in JUDGMENT_FLOORS[judgment]
                       and (judgment != "predetermined" or explicit_profile == matrix_profile))
        reason = ("explicit-profile-choice" if not recommended and explicit_profile != TOP_PROFILE
                  else "explicit-top-exception" if explicit_profile == TOP_PROFILE
                  else "important-explicit-deep-additional-judgment-headroom"
                  if judgment == "important" and explicit_profile == "deep"
                  else "explicit-within-floor")
    return {
        "schema_version": 1,
        "source": source,
        "resolver_version": RESOLVER_VERSION,
        "demand_digest": _digest(normalized),
        "resolved_profile": resolved,
        "judgment_floor": {"predetermined": "none", "important": "balanced-deep",
                           "difficult-uncertain": "deep"}[judgment],
        "reason": reason,

    }


def validate_profile_selection(selection, demand=None, *, profile=None,
                               existing_versioned_stage=False):
    """Recompute every semantic field, rather than trusting a supplied digest."""
    fields = {"schema_version", "source", "resolver_version", "demand_digest",
              "resolved_profile", "judgment_floor", "reason"}
    if (not isinstance(selection, Mapping) or set(selection) != fields
            or type(selection.get("schema_version")) is not int
            or selection["schema_version"] != 1
            or selection.get("resolver_version") != RESOLVER_VERSION):
        raise ModelProfileError("unsupported or invalid profile selection contract",
                                "profile-selection-version-unsupported")
    source = selection.get("source")
    if source not in {"matrix", "explicit", "legacy"}:
        raise ModelProfileError("invalid profile selection source", "profile-selection-invalid")
    resolved = selection.get("resolved_profile")
    if profile is not None and resolved != profile:
        raise ModelProfileError("sealed profile differs from selection", "profile-selection-mismatch")
    expected = resolve_profile_demand(
        demand, explicit_profile=resolved if source in {"explicit", "legacy"} else None,
        legacy=source == "legacy", existing_versioned_stage=existing_versioned_stage,
    )
    if dict(selection) != expected:
        raise ModelProfileError("sealed profile selection differs from resolver",
                                "profile-selection-mismatch")


def load_config(path: str | Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ModelProfileError(f"model profile config unreadable: {exc}") from exc
    for lineno, raw in enumerate(lines, 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            if line.startswith("CFG_"):
                raise ModelProfileError(
                    f"model profile config line {lineno} is a malformed CFG_ declaration"
                )
            continue
        key, value = line.split("=", 1)
        value = value.split("#", 1)[0].strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        key = key.strip()
        if not key.startswith("CFG_"):
            continue
        if not SAFE_KEY.fullmatch(key):
            raise ModelProfileError(
                f"model profile config line {lineno} has an invalid CFG_ key: {key!r}"
            )
        if not value or not SAFE_VALUE.fullmatch(value):
            raise ModelProfileError(
                f"model profile config line {lineno} has an invalid value for {key}"
            )
        values[key] = value
    return values


def resolve_profile_values(
    adapter: str, config: Mapping[str, str], profile: str
) -> dict[str, str]:
    if profile not in KNOWN_PROFILES:
        raise ModelProfileError(f"unknown model profile: {profile!r}")
    if adapter not in {"claude", "codex", "opencode"}:
        raise ModelProfileError(f"unknown adapter: {adapter!r}")
    profile_key = "CFG_MODEL_PROFILE_" + profile.upper().replace("-", "_")
    spec = config.get(profile_key)
    if profile == TOP_PROFILE and not spec:
        # Opt-in only: the selected runtime file (the user's whole-file copy or
        # the shipped default) must say what `top` is; nothing is derived.
        raise ModelProfileError(
            "the selected runtime model config does not declare CFG_MODEL_PROFILE_TOP; "
            "the top exception profile is opt-in", "profile-top-undeclared")
    if not spec or spec.count(":") != 1:
        raise ModelProfileError(f"{profile_key} must declare tier:effort-or-variant")
    tier, budget = spec.split(":", 1)
    tier_key = tier.upper().replace("-", "_")
    model = tier[len("model/"):] if tier.startswith("model/") else config.get(f"CFG_TIER_{tier_key}_MODEL")
    budget_suffix = "VARIANT" if adapter == "opencode" else "EFFORT"
    declared_default = budget if tier.startswith("model/") else config.get(f"CFG_TIER_{tier_key}_{budget_suffix}")
    granularity_key = "CFG_MODEL_PROFILE_GRANULARITY_" + profile.upper().replace("-", "_")
    granularity = config.get(granularity_key) or config.get(
        "CFG_MODEL_PROFILE_GRANULARITY", "unknown"
    )
    if not model or not declared_default:
        if profile == TOP_PROFILE:
            raise ModelProfileError(
                f"{profile_key} names tier {tier!r} but CFG_TIER_{tier_key}_MODEL/"
                f"{budget_suffix} is not declared; the top exception profile is opt-in",
                "profile-top-undeclared")
        raise ModelProfileError(f"profile tier {tier!r} lacks model/{budget_suffix.lower()}")
    if not budget:
        raise ModelProfileError(f"profile {profile!r} has an empty execution budget")
    return {
        "profile": profile,
        "tier": tier,
        "model": model,
        "budget": budget,
        "budget_kind": budget_suffix.lower(),
        "granularity": granularity,
    }


def resolve_profile(adapter: str, config_path: str | Path, profile: str) -> dict[str, str]:
    return resolve_profile_values(adapter, load_config(config_path), profile)


def resolve_runtime_profile(
    adapter: str,
    profile: str,
    *,
    runtime: str | Path | None = None,
    environ: dict[str, str] | None = None,
    source_root: str | Path | None = None,
) -> tuple[dict[str, str], object]:
    """Resolve a profile from the complete user file or complete shipped fallback."""
    try:
        from model_config import ModelConfigError, resolve_config
    except ImportError:  # package import in focused unit tests
        from utilities.model_config import ModelConfigError, resolve_config

    try:
        values, receipt = resolve_config(
            adapter, runtime=runtime, environ=environ, source_root=source_root
        )
    except ModelConfigError as exc:
        raise ModelProfileError(f"runtime model config unavailable: {exc}") from exc
    return resolve_profile_values(adapter, values, profile), receipt


def validate_registered_profile(
    profile: str | None,
    *,
    registered_worker: bool,
    dispatch_depth: int,
    worker_type: str | None,
) -> None:
    if profile is None:
        return
    if profile not in KNOWN_PROFILES:
        raise ModelProfileError(f"unknown model profile: {profile!r}")
    if profile == TOP_PROFILE and not (
        registered_worker and dispatch_depth == 1 and worker_type in TOP_WORKER_TYPES
    ):
        raise ModelProfileError(
            "the top exception profile is limited to a registered dispatch-depth-1 "
            "owner", "profile-top-depth-forbidden")
    if (
        profile == "mini"
        and registered_worker
        and dispatch_depth in {1, 2}
        and worker_type in SUBSTANTIVE_WORKER_TYPES
    ):
        raise ModelProfileError(
            "mini is reserved for lifecycle or explicitly micro-semantic helpers"
        )


def require_top_route(route_file, *, profile: str, node: str | None = None) -> None:
    """The exception profile is a route's decision: a wrapper resolving `top`
    must hold the route that sealed it. Refuses typed when there is no route or
    the route sealed something else (top review B1: without this,
    `--model-profile top` on a route-less depth-1 owner resolved the top model
    with no demand recorded anywhere).

    Which seal is checked depends on WHO is launching, and that is the whole
    point of the `node` parameter:

    - `node is None` -- the caller is the route's owner, so the owner's seal
      (`owner_model_profile`) is the one that authorizes it. Exactly today's
      behavior, unchanged.
    - `node` given -- the caller is a specific node of the route, so THAT
      node's own `model_profile` is checked instead. A frame anchor leg reaches
      `top` while its owner sits at `deep` (see `frame_profile_for_owner`), so
      checking the owner's seal for it would refuse a correctly compiled route.

    Deliberately ONE function rather than two: the two cases differ only in
    which field holds the seal, and a second function is how the two drift
    until one of them forgets to check something."""

    if profile != TOP_PROFILE:
        return
    if not route_file:
        raise ModelProfileError(
            "the top exception profile requires the route that sealed it", "profile-top-route-required")
    try:
        route = json.loads(Path(route_file).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ModelProfileError(f"top route unreadable: {exc}", "profile-top-route-required") from exc
    if not isinstance(route, dict):
        raise ModelProfileError(
            "the route did not seal the top exception profile for its owner", "profile-top-route-mismatch")
    if node is None:
        sealed = route.get("owner_model_profile")
    else:
        entry = next((n for n in route.get("nodes", [])
                      if isinstance(n, dict) and n.get("id") == node), None)
        if entry is None:
            # Loud and distinct: a launch naming a node the route does not
            # declare is a wiring bug, not a policy refusal, and reporting it
            # as a profile mismatch would send the reader to the wrong file.
            raise ModelProfileError(
                f"the route declares no node {node!r} to seal a profile for",
                "profile-top-route-node-unknown")
        sealed = entry.get("model_profile")
    if sealed != TOP_PROFILE:
        raise ModelProfileError(
            "the route did not seal the top exception profile for its owner", "profile-top-route-mismatch")


# The frame bootstrap tier ladder -- ONE function, ONE home.
#
# The frame pair runs one tier ABOVE the owner it frames, because framing is
# the decision the rest of the route cannot revisit. The anchor leg takes that
# raise; the alternative leg stays at the owner's own working tier so the pair
# stays genuinely two-voiced rather than two copies of the same tier.
#
# Do not restate this table anywhere else -- not in `topologies.json`, not in
# `dispatch-defaults.yaml`. `capability-route.py` stamps every frame node's
# `model_profile` from this function at compile time, which is what makes any
# static value in the recipe a placeholder rather than a second home.
FRAME_PROFILE_LADDER = {
    "top": {"anchor": "top", "others": "deep"},
    "deep": {"anchor": "top", "others": "deep"},
    "balanced-deep": {"anchor": "deep", "others": "deep"},
    "balanced": {"anchor": "balanced-deep", "others": "balanced-deep"},
    "light": {"anchor": "balanced", "others": "balanced"},
}
# `top` is not portable, so a frame anchor that lands on it cannot be sealed
# through the legacy "explicit portable profile, no demand" path -- the
# resolver requires a full demand for it. The demand below is a property of the
# frame SHAPE, not of any one task: a framing step is difficult-uncertain by
# construction (it exists precisely because the right direction is not yet
# known) and short-local in execution (its output is one direction brief).
# The compiler uses the owner's own demand when the caller supplied one, and
# falls back to this shape demand otherwise; the reasons say plainly that this
# is the shape speaking, so nothing here reads as task-specific evidence that
# was never gathered.
FRAME_ANCHOR_SHAPE_DEMAND = {
    "schema_version": DEMAND_SCHEMA_VERSION,
    "judgment_requirement": "difficult-uncertain",
    "execution_scope": "short-local",
    "judgment_reason": (
        "framing is the route's one irreversible judgment: every later node "
        "inherits the direction this leg picks, and no later stage is scoped "
        "to re-open it"
    ),
    "execution_reason": (
        "one direction brief, written once, with no multi-step execution of "
        "its own"
    ),
    "evidence_refs": ["roles/units/plan/frame.md"],
}


def frame_profile_for_owner(owner_profile: str) -> dict:
    """Map an owner's resolved profile to its frame pair's two profiles.

    Returns `{"anchor": <profile>, "others": <profile>}`. Unknown or absent
    owner profiles fall back to the `light` rung rather than raising: this runs
    inside route compilation for every recipe, and a route that framed nothing
    is worse than a route framed conservatively."""

    return dict(FRAME_PROFILE_LADDER.get(owner_profile or "light",
                                         FRAME_PROFILE_LADDER["light"]))


def selection_receipt(args):
    """Bounded diagnostic projection of the route already checked by the wrapper."""
    binding = getattr(args, "owner_route_binding", None)
    path = getattr(args, "route_file", None) or getattr(binding, "route_file", None)
    if not path:
        return {}
    route = json.loads(Path(path).read_text(encoding="utf-8"))
    node_id = getattr(args, "route_node", None)
    if node_id:
        node = next((n for n in route.get("nodes", []) if n.get("id") == node_id), None)
        if node is None:
            raise ModelProfileError("profile route node missing", "profile-selection-mismatch")
        selection, demand = node.get("profile_selection"), node.get("profile_demand")
    else:
        selection, demand = route.get("owner_profile_selection"), route.get("owner_profile_demand")
    if route.get("profile_selection_contract_version") is None:
        return {"profile_selection_source": "legacy", "profile_resolver_version": "-",
                "profile_demand_digest": "-", "profile_judgment_floor": "unknown"}
    if type(route["profile_selection_contract_version"]) is not int or route["profile_selection_contract_version"] != 1:
        raise ModelProfileError("unsupported profile selection contract", "profile-selection-version-unsupported")
    validate_profile_selection(selection, demand,
        profile=args.resolved_model_settings["profile"], existing_versioned_stage=True)
    return {"profile_selection_source": selection["source"],
            "profile_resolver_version": selection["resolver_version"],
            "profile_demand_digest": selection["demand_digest"] or "-",
            "profile_selection_digest": _digest(selection),
            "profile_judgment_floor": selection["judgment_floor"]}
