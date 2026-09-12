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
from owner_route_binding import OwnerRouteBindingError, validate_owner_route_binding, derive_quick_owner_binding, derive_frame_route_binding


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
# Captured for validation but not required: `--unit` is meaningless for an owner
# (the tuple contract pins it to `_kernel/owner`) and mandatory for the SD-OPEN-40
# review launch below.
_CAPTURED = _REQUIRED | {"--unit", "--review-output", "--model-role"}
# SD-OPEN-40: the depth-1 tuples this selector may launch. `review` exists so an
# independent reviewer can be a *registered review worker* instead of an owner
# wearing a reviewer's prompt. Before it, every ad-hoc independent review landed
# in the registry as `worker_type=owner`, which is exactly the self-declaration
# SD-OPEN-41(b)'s marker gate has to reject -- the degraded path was the only
# reachable one because no normal path existed.
# Frame nodes are depth-1 advisory workers. Both public node dispatch and
# direct owner selection use this selector; the adapter's atomic attempt claim
# remains the single registration authority.
_LAUNCHABLE_WORKER_TYPES = {"owner", "review", "frame"}
# Legacy route-free frame calls must supply their own scope. Route-bound calls
# derive it from the producer, so inherited/partial environment is not authority.
_FRAME_ARTIFACT_ENV = (
    "AGENT_ARTIFACT_ROOT", "AGENT_ARTIFACT_CAMPAIGN_ID",
    "AGENT_ARTIFACT_CYCLE_ID", "AGENT_ARTIFACT_CYCLE_DIR",
)
_UNIT_REF = re.compile(r"^[a-z-]+/[a-z-]+$")
_RESERVED_UNITS = {"_kernel/owner", "_kernel/resource"}


# Everything the owner tuple needs that a sealed route already states. With
# `--route-evidence` the caller passes only the prompt; an explicit flag still
# wins, and one that contradicts a sealed field is refused typed rather than
# forwarded to a wrapper that would refuse it later with less context.
_ROUTE_FIELDS = {
    "--worktree": "cwd", "--slug": "slug", "--capability": "capability",
    "--capability-mode": "capability_mode", "--intensity": "effective_intensity",
    "--model-profile": "owner_model_profile",
}
_ROUTE_SEALED = {"--worktree", "--capability", "--capability-mode", "--intensity"}
_HINTS = {
    "missing-required": "with --route-evidence <route.json> pass only --prompt-file <file> (or --prompt-text); "
                        "without it: --worktree --slug --capability --capability-mode --qa <level> --intensity --dispatch-depth 1 "
                        "--worker-type owner --owner <capability> --assigned-contract <capability> "
                        "--model-profile deep|balanced-deep|balanced|light (top only from a route that seals it)",
    "route-evidence-arg-mismatch": "omit that flag or pass the route's own value; the route seals it",
    "route-evidence-direct-route-has-no-owner": "a direct route runs inline; compose --shape solo (quick) or staged to get an owner",
    "route-evidence-unreadable": "pass the route *file* printed by compose as route_file=, not the route id",
    "explicit-adapter-outside-route-evidence": "drop --adapter; the route's sealed candidates decide. To change them, recompose: "
                                               "solo/quick with --children <harness>, staged with --parent-harness <harness>",
    "route-evidence-candidates-outside-policy": "the route sealed no harness this user's policy admits (configured_candidates= above is empty): "
                                                "your dispatch-defaults.yaml enables <policy-harnesses> for this model profile. Recompose the route "
                                                "for one of those (solo/quick: --children <harness>; staged: --parent-harness <harness>), or add the "
                                                "harness to harnesses.enabled and this profile's quality bands first. This is not a usage limit -- "
                                                "see eligibility.* above",
    "no-eligible-route-evidence-candidate": "no sealed candidate is usable: it is usage-limited, gated, or has no positive capacity score "
                                            "(see eligibility.* and capacity_headroom.* above). Recompose the route for another harness "
                                            "(solo/quick: --children <harness>; staged: --parent-harness <harness>) or wait for the reset",
    "no-eligible-candidate": "no configured owner harness is usable: usage-limited, gated, or no positive capacity score "
                             "(see eligibility.* and capacity_headroom.* above; utilities/usage-check.sh --harness all)",
    "exactly-one-action-required": "pass exactly one of --dry-run | --register | --start",
    "owner-tuple-required": "the launchable tuple is --dispatch-depth 1 --worker-type owner|review|frame",
    "invalid-model-profile": "--model-profile deep|balanced-deep|balanced|light (top only from a route that seals it)",
    "profile-top-route-required": "drop --model-profile top: the top exception profile is sealed by a route "
                                  "(compose/compile --profile-demands '{\"__owner__\": …}' --explicit-profiles "
                                  "'{\"__owner__\": \"top\"}') and reaches the owner through --route-evidence only",
    "review-worker-unit-required": "--worker-type review needs --unit <catalog persona from roles/units/>",
    "review-worker-route-evidence-unsupported": "a route node's reviewer is launched by stage dispatch; drop --route-evidence for an ad-hoc review worker",
    "review-output-frame-forbidden": "a frame worker returns its advisory verdict through the ordinary dispatch handoff; drop --review-output",
    "frame-artifact-scope-missing": "export all four of AGENT_ARTIFACT_ROOT, AGENT_ARTIFACT_CAMPAIGN_ID, AGENT_ARTIFACT_CYCLE_ID "
                                    "and AGENT_ARTIFACT_CYCLE_DIR in the same Bash call as the launch (OPERATIONS §5.10b)",
    "route-node-unknown": "--route-node must name an id present in the sealed route's nodes list",
    "route-node-worker-type-forbidden": "--route-node selects a frame node's own profile and role; only --worker-type frame may use it",
    "forbidden-flag": "model, reasoning, effort, variant and completion-delivery are sealed by the profile and route; remove the flag",
    "explicit-jobs-outside-parent-registry": "drop --jobs: an interactive Claude parent's completion hook trusts only the inherited "
                                             "AGENT_DISPATCH_JOBS (or the installed canonical registry), so an owner started into another "
                                             "registry could never wake this session",
    "canonical-registry-unusable": "the installed canonical registry path exists but is not an absolute, non-symlink "
                                   "regular file (a symlinked jobs.log?); the parent's completion hook will not read it. "
                                   "Restore the real file at that path before starting an owner",
    "inherited-registry-unusable": "AGENT_DISPATCH_JOBS is set but is not an absolute, non-symlink regular file, and the "
                                   "parent's completion hook reads the SESSION's value, not this command's: changing or unsetting "
                                   "it for one Bash call starts an owner the parent can never wake. Fix the variable in the "
                                   "environment the interactive session was started with (or unset it there for the canonical "
                                   "registry), then start a new session",
}


def hint_for(reason):
    """One line that says what to type next; empty when no hint is known."""
    key = str(reason).split(":", 1)[0]
    return _HINTS.get(key, "")


class OwnerError(ValueError):
    pass


def _node_model_settings(route, route_node):
    """A frame uses its sealed node profile and role, independently of the owner."""
    nodes = route.get("nodes")
    node = next(
        (row for row in nodes if isinstance(row, dict) and row.get("id") == route_node),
        None,
    ) if isinstance(nodes, list) else None
    if node is None:
        raise OwnerError("route-node-unknown")
    settings = {"--model-profile": node.get("model_profile"), "--model-role": node.get("role")}
    for flag, value in settings.items():
        if not isinstance(value, str) or not value.strip():
            raise OwnerError(f"route-node-model-setting-missing:{flag}")
    return settings


def _route_defaults(path, route_node=None):
    """Owner-tuple values a sealed route states; None for fields it lacks."""
    try:
        route = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise OwnerError(f"route-evidence-unreadable:{exc}") from exc
    if not isinstance(route, dict):
        raise OwnerError("route-evidence-unreadable:not-an-object")
    if route.get("effective_intensity") == "direct":
        raise OwnerError("route-evidence-direct-route-has-no-owner")
    capability = route.get("capability")
    values = {flag: route.get(key) for flag, key in _ROUTE_FIELDS.items()}
    values.update({
        "--dispatch-depth": "1", "--worker-type": "owner",
        "--owner": capability, "--assigned-contract": capability,
    })
    if route_node is not None:
        values.update(_node_model_settings(route, route_node))
        node = next(row for row in route["nodes"] if row.get("id") == route_node)
        if node.get("unit") == "plan/frame" and node.get("dispatch_depth") == 1:
            values.update({"--worker-type": "frame", "--unit": "plan/frame", "--assigned-contract": "plan/frame"})
    return {flag: (str(value) if value not in (None, "") else None) for flag, value in values.items()}


def _same_value(flag, given, sealed):
    if flag == "--worktree":
        return _resolved_path(given) == _resolved_path(sealed)
    return str(given) == str(sealed)


def _sealed_owner_context(path, *, worker_type="owner"):
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
        rows = (route.get("dispatch_evidence") or {}).get("tuples") or []
        field = "child_harness" if worker_type == "frame" else "parent_harness"
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
    policy = None if worker_type == "frame" else route.get("owner_harness_policy")
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


def export_owner_route_env(child_env, binding):
    """Publish the owner's route identity into the child environment.

    Every owner needs all three, whatever its intensity: the prompt and the
    wrapper args name only the route *id*, so a call that wants the route
    **file** — `artifact_producer.py begin --route` — has nothing to name
    unless this is set. `quick` used to skip it and its owners had to guess the
    path; one helper for both branches keeps them from drifting apart again.
    """

    child_env["AGENT_OWNER_ROUTE_FILE"] = binding.route_file
    child_env["AGENT_OWNER_ROUTE_ID"] = binding.route_id
    child_env["AGENT_OWNER_ROUTE_HASH"] = binding.route_hash
    return child_env


def _caller_harness(env):
    from dispatch_parent_completion import interactive_parent_identity, DispatchContractError
    try:
        return interactive_parent_identity(env)[0] or None
    except DispatchContractError as exc:
        raise OwnerError(exc.reason) from exc


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
    route_node = None
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
        # Selector-only, like --adapter/--route-evidence: a frame launch bound
        # to a route node derives that node's sealed model profile and role.
        # The selected binding later forwards the wrapper's route-node flag.
        if arg == "--route-node":
            if i + 1 >= len(argv):
                raise OwnerError("route-node-missing")
            route_node = argv[i + 1]
            i += 2
            continue
        if arg.startswith("--route-node="):
            route_node = arg.split("=", 1)[1]
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
        if name in _CAPTURED:
            if equal:
                values[name] = value
                forwarded.append(arg)
                i += 1
                # Without this the equal form fell through to the unconditional
                # append below and every `--flag=value` reached the wrapper
                # twice. Harmless while all three wrappers parse these as plain
                # `store`, and one `action="append"` away from not being.
                continue
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
    derived = []
    required = set(_REQUIRED)
    # Owner tuples retain gap-fill compatibility. Frame launches always read
    # their node: a complete caller tuple cannot replace its sealed model axes.
    gaps = [flag for flag in _REQUIRED if not values.get(flag)]
    if route_evidence and (gaps or route_node) and values.get("--worker-type", "owner") in {"owner", "frame"}:
        sealed_flags = _ROUTE_SEALED | ({"--model-profile", "--model-role"} if route_node else set())
        for flag, sealed in _route_defaults(route_evidence, route_node).items():
            if values.get(flag):
                if flag in sealed_flags and sealed is not None and not _same_value(flag, values[flag], sealed):
                    raise OwnerError(f"route-evidence-arg-mismatch:{flag}")
                continue
            if sealed is None:
                continue
            values[flag] = sealed
            forwarded.extend((flag, sealed))
            derived.append(flag)
        # The wrapper derives --qa from --intensity when it is absent
        # (CONVENTIONS §1.1); a route-backed launch need not repeat it.
        required.discard("--qa")
    missing = sorted(flag for flag in required if not values.get(flag))
    if missing:
        raise OwnerError("missing-required:" + ",".join(missing))
    if len(actions) != 1:
        raise OwnerError("exactly-one-action-required")
    worker_type = values["--worker-type"]
    if values["--dispatch-depth"] != "1" or worker_type not in _LAUNCHABLE_WORKER_TYPES:
        raise OwnerError("owner-tuple-required")
    if route_node and worker_type != "frame":
        raise OwnerError("route-node-worker-type-forbidden")
    unit = (values.get("--unit") or "").strip()
    if worker_type == "owner":
        # Unchanged: the owner tuple pins its own unit downstream
        # (`dispatch_mode_contract`), and an owner that names one is a
        # contradiction the tuple contract already refuses.
        if unit and unit != "_kernel/owner":
            raise OwnerError("invalid-owner-unit")
    else:
        # A review or frame worker is a *unit*, not a kernel role: it must
        # name the catalog persona it runs as, and it can never borrow the
        # owner's. This reuses the same check for both -- SD-OPEN-40's review
        # gate widened, not duplicated, for frame-universal.
        if not unit:
            raise OwnerError("review-worker-unit-required")
        if unit in _RESERVED_UNITS:
            raise OwnerError("review-worker-unit-reserved")
        if not _UNIT_REF.fullmatch(unit):
            raise OwnerError("invalid-review-worker-unit")
        # The comment above says "catalog persona", so check the catalog rather
        # than a shape that `foo/bar` also satisfies.
        if not (ROOT / "roles" / "units" / f"{unit}.md").is_file():
            raise OwnerError("unknown-review-worker-unit")
        if worker_type == "review" and route_evidence:
            # A route node's review worker is launched by the stage dispatcher
            # with its node binding, not by this selector. Accepting route
            # evidence here would let one node be claimed by two launch paths.
            # Frame's public entry points converge here and share one claim.
            raise OwnerError("review-worker-route-evidence-unsupported")
        if worker_type == "frame" and values.get("--review-output"):
            # A frame worker returns an advisory verdict through the ordinary
            # dispatch handoff (roles/worker-types/frame.md), never a durable
            # review report -- --review-output has nothing to bind to here.
            raise OwnerError("review-output-frame-forbidden")
        if worker_type == "frame" and not route_evidence:
            absent = [name for name in _FRAME_ARTIFACT_ENV if not os.environ.get(name)]
            if absent:
                raise OwnerError("frame-artifact-scope-missing:" + ",".join(absent))
        # A report is an explicit opt-in capability.  It is forwarded as a
        # value, never inferred from caller environment or route metadata.
        if values.get("--review-output") and not Path(values["--review-output"]).is_absolute():
            raise OwnerError("review-output-must-be-absolute")
    if worker_type == "owner" and values.get("--review-output"):
        raise OwnerError("review-output-owner-forbidden")
    if values["--model-profile"] not in {"deep", "balanced-deep", "balanced", "light", "top"}:
        raise OwnerError("invalid-model-profile")
    if values["--model-profile"] == "top" and (not route_evidence or "--model-profile" not in derived):
        # The exception profile is a route's decision (a full demand sealed by
        # compose/compile), never a flag's: an explicit `--model-profile top`
        # -- with or without a route -- is refused (top review B1).
        raise OwnerError("profile-top-route-required")
    # Equal-form required options are forwarded unchanged; split-form options
    # were appended above.  Selector-only --adapter/--route-evidence never
    # cross the boundary.
    # W3: carry the selector-only `--route-node` to the quick-binding call site
    # through `values` rather than by widening this return tuple -- every
    # existing caller unpacks five values, and a sixth would break them all.
    # It is never forwarded to the wrapper (the parser above `continue`s past
    # it); the quick binding re-emits its own `--route-node` from the node it
    # actually resolved.
    if route_node:
        values["--route-node"] = route_node
    return explicit, values, forwarded, route_evidence, derived


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


def _usable_registry(path):
    """The hook's own registry predicate (`_validated_jobs`): an absolute,
    non-symlink regular file. One definition of "usable" for the parent's hook
    and the selector that starts owners into it (rewake review R4 M1)."""

    if not path:
        return False
    candidate = Path(path)
    return candidate.is_absolute() and not candidate.is_symlink() and candidate.is_file()


def _authoritative_jobs(values, env):
    """The registry a depth-1 owner is started into, under two parent rules.

    Managed interactive Codex parent: the launcher exported the enrolled
    registry once (a packaged activation root is immutable source, not
    runtime state); a different explicit ``--jobs`` would split the attempt
    graph, so it is refused (`managed-parent-registry-immutable`) and a
    realpath alias of the same file is accepted.

    Interactive Claude parent: its asyncRewake hook trusts exactly one
    registry -- the inherited `AGENT_DISPATCH_JOBS` when the variable is set
    (and only if it is an absolute, non-symlink regular file: an unusable
    value binds nothing there), else the installed canonical registry -- and
    never a file a receipt names. So an unusable inherited value refuses the
    launch (`inherited-registry-unusable`), and an explicit ``--jobs`` that is
    not that trusted file refuses it (`explicit-jobs-outside-parent-registry`)
    -- before spawn, typed and hinted, instead of the hook refusing after the
    owner was sealed `claude-parent-runtime` (rewake reviews R3 M1, R4 M1).

    Any other caller keeps the previous behaviour: explicit, else inherited.
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
    if _caller_harness(env) == "claude":
        if "AGENT_DISPATCH_JOBS" in env:
            if not _usable_registry(inherited):
                raise OwnerError("inherited-registry-unusable")
            trusted = inherited
        else:
            trusted = _canonical_jobs()
            canonical = Path(trusted) if trusted else None
            # The hook trusts the canonical file only as a regular file. A
            # registry that does not exist yet is the ordinary first run (the
            # wrapper creates it); one that exists as a symlink or a
            # non-regular file would be written by the wrapper and never
            # read by the hook (top review M2).
            if canonical is not None and (canonical.is_symlink() or canonical.exists()) \
                    and not _usable_registry(trusted):
                raise OwnerError("canonical-registry-unusable")
        if explicit and (not trusted or _resolved_path(explicit) != _resolved_path(trusted)):
            raise OwnerError("explicit-jobs-outside-parent-registry")
    return explicit or inherited


def _canonical_jobs():
    """The installed harness's own registry path, or "" when it cannot be
    resolved (the caller then refuses rather than guessing)."""
    try:
        from dispatch_contract import resolve_agent_home, resolve_dispatch_state_root
        return str(resolve_dispatch_state_root(resolve_agent_home(), None) / "jobs.log")
    except Exception:  # noqa: BLE001 -- absence beats a guessed registry
        return ""


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
    hint = hint_for(reason)
    if hint:
        lines.append(f"hint={hint}")
    print("\n".join(lines))
    return 65


def main(argv):
    try:
        explicit, values, forwarded, route_evidence, derived = _parse(argv)
        jobs = _authoritative_jobs(values, os.environ)
        profile = values["--model-profile"]
        sealed_context = _sealed_owner_context(route_evidence, worker_type=values["--worker-type"]) if route_evidence else None
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
        policy_harnesses = list(configured)
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
            if sealed is not None and not configured:
                # Nothing was even a candidate: every harness the route sealed
                # sits outside this user's enabled set and quality bands. The
                # old answer here was `no-eligible-route-evidence-candidate`,
                # whose hint blames usage limits, gating, or capacity -- and
                # the audit above prints `eligibility.<harness>=ok` right next
                # to it, so the receipt contradicted itself (2026-09-10, a
                # route sealed for a harness the policy excludes).
                reason = "route-evidence-candidates-outside-policy"
                detail = hint_for(reason).replace(
                    "<policy-harnesses>", ",".join(policy_harnesses) or "none")
            elif sealed is not None:
                reason = "no-eligible-route-evidence-candidate"
                detail = hint_for(reason)
            else:
                reason = "no-eligible-candidate"
                detail = hint_for(reason)
            print(f"check=failed\nreason={reason}\nchild_spawned=0\nhint={detail}")
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
        print(f"route_defaults={','.join(derived) or 'none'}", flush=True)
        child_env = {
            key: value for key, value in os.environ.items() if not _MODEL_ENV.fullmatch(key)
        }
        if values["--worker-type"] in {"review", "frame"}:
            # A direct reviewer is deliberately route-free.  The selector may
            # itself run inside a route-owned owner, so inherited route
            # variables must not silently bind the child to that owner/node.
            for key in (
                "AGENT_OWNER_ROUTE_FILE", "AGENT_OWNER_ROUTE_ID",
                "AGENT_OWNER_ROUTE_HASH", "AGENT_ROUTE_FILE",
                "AGENT_ROUTE_ID", "AGENT_ROUTE_NODE",
            ):
                child_env.pop(key, None)
        caller_harness = _caller_harness(child_env)
        if caller_harness:
            child_env["AGENT_DISPATCH_CALLER_HARNESS"] = caller_harness
        child_env["AGENT_DISPATCH_OWNER_HARNESS"] = selected
        if route_evidence:
            route_data = json.loads(Path(route_evidence).read_text(encoding="utf-8"))
            if values["--worker-type"] == "frame" or route_data.get("effective_intensity") == "quick":
                # W3: quick is a three-node route, so the caller's own
                # `--route-node` selects which node this launch binds to. The
                # default keeps every existing quick OWNER launch identical.
                derive = (derive_frame_route_binding if values["--worker-type"] == "frame"
                          else derive_quick_owner_binding)
                binding = derive(
                    route_evidence, worktree=values["--worktree"],
                    capability=values["--capability"], capability_mode=values["--capability-mode"],
                    intensity=values["--intensity"], harness=selected,
                    route_node=values.get("--route-node") or "one-shot",
                )
                forwarded += ["--route-file", binding.route_file, "--route-id", binding.route_id,
                              "--route-hash", binding.route_hash, "--route-node", binding.route_node,
                              "--registry-digest", binding.registry_digest, "--write-scope", binding.write_scope,
                              "--completion-gate", binding.completion_gate]
                if values["--worker-type"] == "frame":
                    from artifact_producer import prepare_route_artifact_env, ProducerError
                    try:
                        child_env.update(prepare_route_artifact_env(
                            Path(binding.route_file), start="--start" in forwarded, jobs=Path(jobs)))
                    except ProducerError as exc:
                        raise OwnerError(f"{exc.code}:{exc.detail}") from exc
                # Deliberately NOT export_owner_route_env() here. The adapters
                # treat "env binding present" as the discriminator for a
                # standard+ owner and refuse `owner-route-binding-tuple-invalid`
                # when a route file argument arrives alongside it. quick already
                # carries its route through `--route-file`, which reaches the
                # owner in the prompt; exporting the env as well killed every
                # quick owner at launch (regression shipped in v2.109.2).
            else:
                binding = validate_owner_route_binding(
                route_evidence,
                worktree=values["--worktree"],
                capability=values["--capability"],
                capability_mode=values["--capability-mode"],
                intensity=values["--intensity"],
                harness=selected,
            )
                export_owner_route_env(child_env, binding)
            if values["--worker-type"] == "owner":
                from artifact_producer import prepare_route_artifact_env, ProducerError
                try:
                    child_env.update(prepare_route_artifact_env(
                        Path(binding.route_file), start="--start" in forwarded, jobs=Path(jobs)))
                except ProducerError as exc:
                    raise OwnerError(f"{exc.code}:{exc.detail}") from exc
        child = subprocess.run([str(wrapper), *forwarded], env=child_env)
        return child.returncode
    except (OwnerError, OwnerRouteBindingError, OSError) as exc:
        return _error(str(exc))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
