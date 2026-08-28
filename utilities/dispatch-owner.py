#!/usr/bin/env python3
"""Select and launch the configured portable dispatch-depth-1 owner."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from owner_route_binding import OwnerRouteBindingError, validate_owner_route_binding


ROOT = Path(__file__).resolve().parents[1]
_defaults_spec = importlib.util.spec_from_file_location(
    "dispatch_defaults", ROOT / "utilities" / "dispatch-defaults.py"
)
if _defaults_spec is None or _defaults_spec.loader is None:
    raise RuntimeError("cannot load dispatch-defaults.py")
_defaults = importlib.util.module_from_spec(_defaults_spec)
_defaults_spec.loader.exec_module(_defaults)
_allocation_spec = importlib.util.spec_from_file_location(
    "dispatch_allocation", ROOT / "utilities" / "dispatch_allocation.py"
)
if _allocation_spec is None or _allocation_spec.loader is None:
    raise RuntimeError("cannot load dispatch_allocation.py")
_allocation = importlib.util.module_from_spec(_allocation_spec)
_allocation_spec.loader.exec_module(_allocation)
_capacity_spec = importlib.util.spec_from_file_location(
    "harness_capacity", ROOT / "utilities" / "harness-capacity.py"
)
if _capacity_spec is None or _capacity_spec.loader is None:
    raise RuntimeError("cannot load harness-capacity.py")
_capacity = importlib.util.module_from_spec(_capacity_spec)
_capacity_spec.loader.exec_module(_capacity)

_FORBIDDEN = {
    "--worker-mode", "--model", "--reasoning", "--effort", "--variant",
    "--inherit-model-settings", "--completion-delivery",
    "--allow-unmanaged-parent-poll",
}
_MODEL_ENV = re.compile(
    r"^[A-Za-z0-9]+_DISPATCH_(MODEL|MODEL_ROLE|MODEL_PROFILE|REASONING|EFFORT|VARIANT)$"
)
_REQUIRED = {
    "--worktree", "--slug", "--capability", "--capability-mode", "--qa",
    "--intensity", "--dispatch-depth", "--worker-type", "--assigned-contract",
    "--owner", "--model-profile",
}


class OwnerError(ValueError):
    pass


def _sealed_owner_context(path):
    """Return route-sealed owner candidates, quality policy, and allocation.

    An owner is not a route node, so this selector stays route-blind for
    dispatch (`--route-file` to the wrapper is `route-metadata-missing`). But
    the route's dispatch evidence names the harness its dispatch-depth-2 tuples
    expect the owner to be, and nothing bound the two: with
    `configured owners=[claude]` and claude usage `limited`, the eligibility
    cascade would select codex and every depth-2 hop would then fail
    `dispatch-evidence-parent-runtime-mismatch` -- the 2026-08-04 incident with
    the harness field substituted for the transport field. Selector-only and
    optional: without it the cascade is unchanged.
    """

    try:
        route = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise OwnerError(f"route-evidence-unreadable:{exc}") from exc
    intensity = route.get("effective_intensity")
    if intensity == "direct":
        raise OwnerError("route-evidence-direct-route-has-no-owner")
    if intensity == "quick":
        # A quick route seals no depth-2 tuples; its own registered-headless
        # candidates name the harnesses that were probed. The wrapper already
        # rejects a foreign harness there (`quick-headless-unavailable`), so
        # this only moves the same verdict ahead of the launch.
        rows, field = route.get("registered_headless_candidates") or [], "harness"
    else:
        rows, field = (route.get("dispatch_evidence") or {}).get("tuples") or [], "parent_harness"
        if any(
            isinstance(row, dict)
            and row.get("status") == "unsupported"
            and row.get("failure_scope") == "exact-worktree"
            and row.get("retry_on_isolated_worktree") == 1
            for row in rows
        ):
            raise OwnerError("route-evidence-exact-worktree-reprobe-required")
    harnesses = {
        row.get(field)
        for row in rows
        if isinstance(row, dict) and row.get("status") == "supported"
    }
    harnesses &= _defaults.DISPATCHABLE_HARNESSES
    if not harnesses:
        raise OwnerError("route-evidence-no-supported-owner-harness")
    policy = route.get("owner_harness_policy")
    if policy is not None:
        if not isinstance(policy, dict) or any(
            not isinstance(policy.get(band), list)
            for band in _defaults.QUALITY_BANDS
        ):
            raise OwnerError("route-evidence-owner-policy-malformed")
        threshold = policy.get("promote_relief_below")
        if not isinstance(threshold, int) or not 0 <= threshold <= 100:
            raise OwnerError("route-evidence-owner-policy-malformed")
        if not isinstance(route.get("dispatch_allocation"), dict):
            raise OwnerError("route-evidence-owner-allocation-missing")
    return {
        "harnesses": harnesses,
        "policy": policy,
        "allocation": route.get("dispatch_allocation"),
    }


def _sealed_owner_harnesses(path):
    """Compatibility/query view used by diagnostics and tests."""
    return _sealed_owner_context(path)["harnesses"]


def _caller_harness(env):
    """Keep the interactive caller distinct from the selected child adapter."""

    explicit = env.get("AGENT_DISPATCH_CALLER_HARNESS") or env.get(
        "AGENT_DISPATCH_CURRENT_HARNESS"
    )
    if explicit:
        if explicit not in _defaults.DISPATCHABLE_HARNESSES:
            raise OwnerError("caller-harness-invalid")
        return explicit
    detected = set()
    if env.get("CODEX_THREAD_ID") or env.get("CODEX_SESSION_ID"):
        detected.add("codex")
    if env.get("CLAUDE_CODE_SESSION_ID"):
        detected.add("claude")
    if env.get("OPENCODE_SESSION_ID"):
        detected.add("opencode")
    if len(detected) > 1:
        raise OwnerError("caller-harness-ambiguous")
    return next(iter(detected), None)


def _load_defaults():
    config_path = _defaults.default_config_path()
    try:
        config = _defaults.load_and_validate(config_path, _defaults.default_topology_path())
    except (OSError, ValueError, _defaults.DefaultsConfigError) as exc:
        raise OwnerError(f"defaults-invalid:{exc}") from exc
    return config


def _parse(argv):
    if argv == ["--help"] or not argv:
        print("usage: dispatch-owner [--adapter <harness>] --dry-run|--register|--start ...")
        raise SystemExit(0)
    forwarded = []
    explicit = None
    route_evidence = None
    values = {}
    actions = []
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--adapter":
            if i + 1 >= len(argv):
                raise OwnerError("adapter-missing")
            explicit = argv[i + 1]
            i += 2
            continue
        if arg.startswith("--adapter="):
            explicit = arg.split("=", 1)[1]
            i += 1
            continue
        # Selector-only, like --adapter: consumed here and never forwarded, so
        # the wrapper still sees an owner launch with no route node.
        if arg == "--route-evidence":
            if i + 1 >= len(argv):
                raise OwnerError("route-evidence-missing")
            route_evidence = argv[i + 1]
            i += 2
            continue
        if arg.startswith("--route-evidence="):
            route_evidence = arg.split("=", 1)[1]
            i += 1
            continue
        name, equal, value = arg.partition("=")
        if name in {"--dry-run", "--register", "--start"}:
            if equal:
                raise OwnerError(f"invalid-action:{arg}")
            actions.append(name)
            forwarded.append(arg)
            i += 1
            continue
        if name in _FORBIDDEN or (equal and name in _FORBIDDEN):
            raise OwnerError(f"forbidden-flag:{name}")
        if name in _REQUIRED:
            if equal:
                values[name] = value
                forwarded.append(arg)
            else:
                if i + 1 >= len(argv) or argv[i + 1].startswith("--"):
                    raise OwnerError(f"missing-value:{name}")
                values[name] = argv[i + 1]
                forwarded.extend((arg, argv[i + 1]))
                i += 2
                continue
        if name == "--jobs":
            if equal:
                values[name] = value
            else:
                if i + 1 >= len(argv) or argv[i + 1].startswith("--"):
                    raise OwnerError("missing-value:--jobs")
                values[name] = argv[i + 1]
            forwarded.append(arg)
            i += 1
            continue
        forwarded.append(arg)
        i += 1
    missing = sorted(flag for flag in _REQUIRED if not values.get(flag))
    if missing:
        raise OwnerError("missing-required:" + ",".join(missing))
    if len(actions) != 1:
        raise OwnerError("exactly-one-action-required")
    if values["--dispatch-depth"] != "1" or values["--worker-type"] != "owner":
        raise OwnerError("owner-tuple-required")
    if values["--model-profile"] not in {"deep", "balanced-deep", "light"}:
        raise OwnerError("invalid-model-profile")
    # Equal-form required options are forwarded unchanged; split-form options
    # were appended above.  Selector-only --adapter/--route-evidence never
    # cross the boundary.
    return explicit, values, forwarded, route_evidence


def _eligible(state):
    """Return hard eligibility for an explicit user-selected adapter.

    `unknown` is not a positive automatic capacity signal, but it remains a
    valid explicit override when the route and user policy authorize it.
    """

    return state != "limited" and not state.startswith("limited(")


def _usage(jobs):
    cmd = [str(ROOT / "utilities" / "usage-check.sh"), "--harness", "all"]
    if jobs:
        cmd += ["--jobs", jobs]
    result = subprocess.run(cmd, text=True, capture_output=True, env=os.environ.copy())
    if result.returncode != 0:
        raise OwnerError("usage-check-failed")
    states = {}
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) == 2 and fields[0] in _defaults.DISPATCHABLE_HARNESSES:
            if fields[0] in states:
                raise OwnerError("eligibility-malformed")
            states[fields[0]] = fields[1]
    if set(states) != set(_defaults.DISPATCHABLE_HARNESSES):
        raise OwnerError("eligibility-malformed")
    return states


def _resolved_path(value):
    return Path(value).expanduser().resolve(strict=False)


def _authoritative_jobs(values, env):
    """Preserve the registry selected by a managed interactive parent.

    A packaged activation root is immutable source, not runtime state.  The
    managed launcher exports the enrolled registry once; accepting a different
    depth-1 ``--jobs`` value would split the attempt graph before the selected
    adapter gets a chance to validate it.
    """

    explicit = values.get("--jobs", "")
    inherited = env.get("AGENT_DISPATCH_JOBS", "")
    managed = (
        env.get("AGENT_CODEX_MANAGED_GATEWAY") == "1"
        and env.get("AGENT_CODEX_MANAGED_PARENT_RUNTIME") == "codex"
    )
    if managed and inherited:
        if explicit and _resolved_path(explicit) != _resolved_path(inherited):
            raise OwnerError("managed-parent-registry-immutable")
        return inherited
    return explicit or inherited


def _audit(
    status, adapter, source, configured, explicit, states, *, allocation=None,
    counts=None, rejected=(), fallback=None, reason="none", capacity=None,
    quality_band=None, relief_promoted=False,
):
    lines = [
        f"status={status}", f"adapter={adapter or '-'}", f"selection_source={source}",
        f"configured_candidates={','.join(configured)}",
        f"explicit_adapter={explicit or 'none'}",
    ]
    for harness in sorted(states):
        lines.append(f"eligibility.{harness}={states[harness]}")
    if allocation:
        lines.append(f"allocation_strategy={allocation['strategy']}")
        lines.append(f"allocation_window={allocation['window']}")
        lines.append(f"usage_gate_used_percent={allocation.get('usage_gate_used_percent', 90)}")
        affinity = allocation.get("depth_affinity") or {}
        lines.append("depth_affinity=" + (",".join(f"{key}:{affinity[key]}" for key in sorted(affinity)) or "none"))
        lines.append(f"depth_affinity_weight={allocation.get('depth_affinity_weight', 0.5)}")
        lines.append(f"usage_headroom_exponent={allocation.get('usage_headroom_exponent', 1)}")
    for harness in _allocation.HARNESSES:
        if counts is not None:
            lines.append(f"attempt_count.{harness}={counts.get(harness, 0)}")
        if capacity is not None:
            value = capacity.get(harness)
            lines.append(
                f"capacity_headroom.{harness}="
                + ("unknown" if value is None else str(round(value, 1)))
            )
    if quality_band:
        lines.append(f"quality_band={quality_band}")
    lines.append(f"relief_promoted={int(relief_promoted)}")
    for n, item in enumerate(rejected, 1):
        lines.append(f"rejected.{n}={item}:usage-{states[item]}")
    if fallback:
        lines.append(f"fallback.1={fallback}:configured-candidates-ineligible")
    lines += [
        "trace.1=cascade=explicit>hard-eligibility>quality-band>capacity>recent-attempt-balance",
        f"trace.2=explicit={explicit or 'none'};authorized={int(bool(explicit and explicit in _defaults.DISPATCHABLE_HARNESSES))}",
        "trace.3=eligibility=" + ",".join(f"{h}:{states[h]}" for h in sorted(states)),
        f"trace.4=configured={','.join(configured)};selected={adapter or '-'};source={source};deviation_reason={reason}",
    ]
    return lines


def _error(reason, configured=(), explicit=None, states=None):
    lines = _audit("unavailable", None, "none", configured, explicit, states or {})
    lines += [f"check=failed", f"reason={reason}", "child_spawned=0"]
    print("\n".join(lines))
    return 65


def main(argv):
    try:
        explicit, values, forwarded, route_evidence = _parse(argv)
        jobs = _authoritative_jobs(values, os.environ)
        profile = values["--model-profile"]
        sealed_context = _sealed_owner_context(route_evidence) if route_evidence else None
        if sealed_context and isinstance(sealed_context.get("policy"), dict):
            policy = dict(sealed_context["policy"])
            config = None
            config_version = 3
        else:
            config = _load_defaults()
            policy = _defaults.query_profile_policy(config, profile)
            config_version = config.get("schema_version")
        configured = [
            harness for band in _defaults.QUALITY_BANDS for harness in policy[band]
        ]
        # Legacy schema-v1 still admits an explicit OpenCode relief request;
        # schema-v3 authorization comes from the enabled set and quality bands.
        if explicit is not None and explicit not in _defaults.DISPATCHABLE_HARNESSES:
            raise OwnerError("explicit-adapter-unauthorized")
        if (
            explicit is not None
            and config_version == 3
            and explicit not in configured
        ):
            raise OwnerError("explicit-adapter-disabled-by-user-policy")
        sealed = sealed_context["harnesses"] if sealed_context else None
        if sealed is not None:
            if explicit is not None and explicit not in sealed:
                raise OwnerError("explicit-adapter-outside-route-evidence")
            configured = [h for h in configured if h in sealed]
            policy = {
                **policy,
                **{
                    band: [h for h in policy[band] if h in sealed]
                    for band in _defaults.QUALITY_BANDS
                },
            }
        states = _usage(jobs)
        allocation = (
            sealed_context.get("allocation")
            if sealed_context and isinstance(sealed_context.get("allocation"), dict)
            else _defaults.query_allocation(config)
        )
        counts = (
            _allocation.attempt_counts(jobs, window=allocation["window"])
            if allocation["strategy"] in {_allocation.STRATEGY, "capacity-aware", "balanced"}
            else {harness: 0 for harness in _allocation.HARNESSES}
        )

        def ranked(candidates):
            candidates = list(candidates)
            if allocation["strategy"] != _allocation.STRATEGY:
                return candidates
            return _allocation.rank_harnesses(
                candidates,
                counts,
                declared_order=allocation["harness_order"],
            )

        rejected = [h for h in sorted(states) if not _eligible(states[h])]
        capacity = _capacity.capacity_scores()

        def automatically_available(harness):
            score = capacity.get(harness)
            return _eligible(states[harness]) and score is not None and score > 0

        selected = None
        source = "none"
        reason = "none"
        quality_band = None
        relief_promoted = False
        if explicit and _eligible(states[explicit]):
            selected, source, quality_band = explicit, "explicit", "explicit"
        if selected is None and config_version == 3:
            selected, quality_band, _ranks, relief_promoted = _capacity.select(
                policy, states, counts, allocation["harness_order"], capacity,
                strategy=allocation["strategy"],
                usage_gate_used_percent=allocation.get("usage_gate_used_percent", 90),
                preferred=_capacity.preferred_for_depth(allocation, 1),
                affinity_weight=allocation.get("depth_affinity_weight", 0.5),
                headroom_exponent=allocation.get("usage_headroom_exponent", 1),
            )
            if selected:
                source = "configured-" + allocation["strategy"]
        if selected is None and config_version != 3:
            for harness in ranked(configured):
                if automatically_available(harness):
                    selected = harness
                    source = (
                        "configured-usage-balanced"
                        if allocation["strategy"] == _allocation.STRATEGY
                        else "configured-normal"
                    )
                    quality_band = "primary"
                    break
        if selected is None:
            # A sealed route constrains this last resort too: silently starting
            # an owner whose harness the checked tuples never probed only moves
            # the failure to every dispatch-depth-2 launch.
            if config_version == 3:
                fallback_pool = ()
            elif sealed is not None:
                fallback_pool = sealed
            elif config_version == 2:
                fallback_pool = _defaults.DISPATCHABLE_HARNESSES
            else:
                fallback_pool = _defaults.LEGACY_NORMAL_HARNESSES
            for harness in ranked(fallback_pool):
                if automatically_available(harness):
                    selected, source, reason = harness, "eligibility-fallback", "configured-candidates-ineligible"
                    quality_band = "outside-policy-fallback"
                    break
        if selected is None:
            print("\n".join(_audit(
                "unavailable", None, "none", configured, explicit, states,
                allocation=allocation, counts=counts, rejected=rejected,
                capacity=capacity, relief_promoted=relief_promoted,
            )))
            print("check=failed\nreason=" + (
                "no-eligible-route-evidence-candidate" if sealed is not None
                else "no-eligible-candidate"
            ) + "\nchild_spawned=0")
            return 65
        wrapper = ROOT / "adapters" / selected / "bin" / "dispatch-headless.py"
        if not os.access(wrapper, os.X_OK):
            print("\n".join(_audit("unavailable", selected, source, configured, explicit, states,
                                      allocation=allocation, counts=counts,
                                      rejected=rejected if source != "explicit" else (),
                                      fallback=selected if source == "eligibility-fallback" else None,
                                      reason=reason, capacity=capacity,
                                      quality_band=quality_band,
                                      relief_promoted=relief_promoted)))
            print("check=failed\nreason=wrapper-unavailable\nchild_spawned=0")
            return 65
        print("\n".join(_audit("eligible", selected, source, configured, explicit, states,
                                  allocation=allocation, counts=counts,
                                  rejected=rejected if source != "explicit" else (),
                                  fallback=selected if source == "eligibility-fallback" else None,
                                  reason=reason, capacity=capacity,
                                  quality_band=quality_band,
                                  relief_promoted=relief_promoted)), flush=True)
        child_env = {
            key: value for key, value in os.environ.items() if not _MODEL_ENV.fullmatch(key)
        }
        caller_harness = _caller_harness(child_env)
        if caller_harness:
            child_env["AGENT_DISPATCH_CALLER_HARNESS"] = caller_harness
        child_env["AGENT_DISPATCH_OWNER_HARNESS"] = selected
        if route_evidence:
            binding = validate_owner_route_binding(
                route_evidence,
                worktree=values["--worktree"],
                capability=values["--capability"],
                capability_mode=values["--capability-mode"],
                intensity=values["--intensity"],
                harness=selected,
            )
            child_env["AGENT_OWNER_ROUTE_FILE"] = binding.route_file
            child_env["AGENT_OWNER_ROUTE_ID"] = binding.route_id
            child_env["AGENT_OWNER_ROUTE_HASH"] = binding.route_hash
        child = subprocess.run([str(wrapper), *forwarded], env=child_env)
        return child.returncode
    except (OwnerError, OwnerRouteBindingError, OSError) as exc:
        return _error(str(exc))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
