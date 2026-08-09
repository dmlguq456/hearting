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


def _sealed_owner_harnesses(path):
    """Return the parent harnesses a standard+ route's checked evidence supports.

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
    return harnesses


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
    """Match dispatch-route.sh's eligible(): only limited(...) is a hard failure.

    `unknown` (jobs.log unavailable) and `ok` both remain candidates, per
    usage-check.sh's documented contract that `unknown` means "the
    orchestrator decides," not a failure.
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


def _audit(status, adapter, source, configured, explicit, states, rejected=(), fallback=None, reason="none"):
    lines = [
        f"status={status}", f"adapter={adapter or '-'}", f"selection_source={source}",
        f"configured_candidates={','.join(configured)}",
        f"explicit_adapter={explicit or 'none'}",
    ]
    for harness in sorted(states):
        lines.append(f"eligibility.{harness}={states[harness]}")
    for n, item in enumerate(rejected, 1):
        lines.append(f"rejected.{n}={item}:usage-{states[item]}")
    if fallback:
        lines.append(f"fallback.1={fallback}:configured-candidates-ineligible")
    lines += [
        "trace.1=cascade=explicit>hard-eligibility>configured-normal",
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
        config = _load_defaults()
        configured = list(_defaults.query_owners(config))
        # DISPATCHABLE (not KNOWN): SD-66 relief-only policy names an explicit
        # --adapter opencode as a legitimate relief path; opencode stays out of
        # the configured/last-resort candidate sets below.
        if explicit is not None and explicit not in _defaults.DISPATCHABLE_HARNESSES:
            raise OwnerError("explicit-adapter-unauthorized")
        sealed = _sealed_owner_harnesses(route_evidence) if route_evidence else None
        if sealed is not None:
            if explicit is not None and explicit not in sealed:
                raise OwnerError("explicit-adapter-outside-route-evidence")
            configured = [h for h in configured if h in sealed]
        jobs = values.get("--jobs", os.environ.get("AGENT_DISPATCH_JOBS", ""))
        states = _usage(jobs)
        rejected = [h for h in sorted(states) if not _eligible(states[h])]
        selected = None
        source = "none"
        reason = "none"
        if explicit and _eligible(states[explicit]):
            selected, source = explicit, "explicit"
        if selected is None:
            for harness in configured:
                if _eligible(states[harness]):
                    selected, source = harness, "configured-normal"
                    break
        if selected is None:
            # A sealed route constrains this last resort too: silently starting
            # an owner whose harness the checked tuples never probed only moves
            # the failure to every dispatch-depth-2 launch.
            for harness in sorted(sealed if sealed is not None else _defaults.KNOWN_HARNESSES):
                if _eligible(states[harness]):
                    selected, source, reason = harness, "eligibility-fallback", "configured-candidates-ineligible"
                    break
        if selected is None:
            print("\n".join(_audit("unavailable", None, "none", configured, explicit, states, rejected=rejected)))
            print("check=failed\nreason=" + (
                "no-eligible-route-evidence-candidate" if sealed is not None
                else "no-eligible-candidate"
            ) + "\nchild_spawned=0")
            return 65
        wrapper = ROOT / "adapters" / selected / "bin" / "dispatch-headless.py"
        if not os.access(wrapper, os.X_OK):
            print("\n".join(_audit("unavailable", selected, source, configured, explicit, states,
                                      rejected=rejected if source != "explicit" else (),
                                      fallback=selected if source == "eligibility-fallback" else None,
                                      reason=reason)))
            print("check=failed\nreason=wrapper-unavailable\nchild_spawned=0")
            return 65
        print("\n".join(_audit("eligible", selected, source, configured, explicit, states,
                                  rejected=rejected if source != "explicit" else (),
                                  fallback=selected if source == "eligibility-fallback" else None,
                                  reason=reason)), flush=True)
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
