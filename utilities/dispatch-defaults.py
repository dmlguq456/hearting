#!/usr/bin/env python3
"""Strict standard-library loader/validator/query CLI for harness routing policy.

Parses a deliberately narrow YAML subset (scalars, inline lists, nested
mappings, comments) — no PyYAML/yq dependency. Validates against the
canonical topology node ids in capabilities/topologies.json and exposes
affinity/owner/allocation/quality-band queries.  Schemas v1/v2 remain readable;
schema v3 adds user-local enabled harnesses and per-profile quality bands;
schema v4 adds `confirmation.mode` and `steward.child_permission_mode`.
"""
import json
import os
import sys

LEGACY_NORMAL_HARNESSES = {"claude", "codex"}
DISPATCHABLE_HARNESSES = {"claude", "codex", "opencode"}
KNOWN_HARNESSES = DISPATCHABLE_HARNESSES
AFFINITY_VALUES = {"claude", "codex", "opencode", "diverse"}
MODEL_PROFILES = ("deep", "balanced-deep", "light", "mini")
QUALITY_BANDS = ("primary", "relief", "last_resort")
ALLOCATION_STRATEGIES = {"least-recent-attempts", "capacity-aware", "balanced"}
DEFAULT_USAGE_GATE_USED_PERCENT = 90
TOP_LEVEL_KEYS = {
    "schema_version", "depth1_owner", "opencode", "allocation", "capabilities",
    "harnesses", "profiles", "headless", "confirmation", "steward",
}
# core/OPERATIONS.md §5.10 "Registered headless permission posture": the
# Claude wrapper pins the starting permission mode of every registered
# `claude -p` turn. `bypass` appends `--permission-mode bypassPermissions`;
# `allowlist` keeps the runtime's starting mode and pre-approves only the
# harness utilities. The shipped default is `bypass`; omitting the section
# selects it.
HEADLESS_KEYS = {"claude_permission_mode"}
HEADLESS_PERMISSION_MODES = ("bypass", "allowlist")
DEFAULT_HEADLESS_PERMISSION_MODE = "bypass"
CONFIRMATION_MODES = ("hybrid", "both", "post-frame-only")
DEFAULT_CONFIRMATION_MODE = "hybrid"
STEWARD_CHILD_PERMISSION_MODES = ("bypass", "inherit")
DEFAULT_STEWARD_CHILD_PERMISSION_MODE = "bypass"


class DefaultsConfigError(Exception):
    pass


sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
from dispatch_allocation import inert_allocation_keys  # noqa: E402


def _repo_root():
    # realpath, not abspath: the helper is projected into
    # adapters/<harness>/utilities/ as a symlink, and the shipped config and
    # topology registry live only at the real repo root.
    return os.path.dirname(os.path.dirname(os.path.realpath(__file__)))


def default_config_path():
    override = os.environ.get("DISPATCH_DEFAULTS_CONFIG")
    if override:
        return override
    config_home = os.environ.get("XDG_CONFIG_HOME")
    if not config_home:
        config_home = os.path.join(os.path.expanduser("~"), ".config")
    user_path = os.path.join(config_home, "hearting", "dispatch-defaults.yaml")
    if os.path.isfile(user_path):
        return user_path
    return os.path.join(_repo_root(), "profiles", "dispatch-defaults.yaml")


def default_topology_path():
    return os.path.join(_repo_root(), "capabilities", "topologies.json")


SHIPPED_CONFIG_PATH = os.path.join(_repo_root(), "profiles", "dispatch-defaults.yaml")


def shipped_capability_baseline():
    """Return the shipped profiles/dispatch-defaults.yaml `capabilities` mapping.

    `{}` when the shipped file is absent. A malformed shipped file raises
    DefaultsConfigError from the parser; the caller wraps it as a named,
    fail-loud "corrupt shipped baseline" condition rather than silently
    reproducing the defect this repairs.
    """
    if not os.path.isfile(SHIPPED_CONFIG_PATH):
        return {}
    with open(SHIPPED_CONFIG_PATH, encoding="utf-8") as f:
        text = f.read()
    shipped = parse_yaml_subset(text)
    caps = shipped.get("capabilities")
    return caps if isinstance(caps, dict) else {}


def merge_capability_baseline(config, capmap, baseline=None):
    """Merge shipped capability affinity cells beneath the user's config.

    A user cell always wins; an absent user cell inherits the baseline value.
    A baseline cell is dropped, never an error, when its capability or stage is
    unknown to `capmap`, its value is outside AFFINITY_VALUES, or its value
    names a concrete harness the user has disabled. Iterates sorted keys so
    the merged object is deterministic — `canonical()` in capability-route.py
    sorts keys too, so `_seal_dispatch_defaults`'s digest over this merged
    object is deterministic for free.
    """
    baseline = shipped_capability_baseline() if baseline is None else baseline
    enabled = set(query_owners(config))
    merged_caps = {
        cap: dict(stagemap) if isinstance(stagemap, dict) else {}
        for cap, stagemap in (config.get("capabilities") or {}).items()
    }
    for cap_name in sorted(baseline):
        if cap_name not in capmap:
            continue
        stagemap = baseline.get(cap_name)
        if not isinstance(stagemap, dict):
            continue
        for stage_name in sorted(stagemap):
            if stage_name not in capmap[cap_name]:
                continue
            value = stagemap[stage_name]
            if value not in AFFINITY_VALUES:
                continue
            if value != "diverse" and value not in enabled:
                continue
            merged_caps.setdefault(cap_name, {})
            merged_caps[cap_name].setdefault(stage_name, value)
    merged = dict(config)
    merged["capabilities"] = merged_caps
    return merged


def _strip_comment(line):
    in_quote = None
    out = []
    for ch in line:
        if in_quote:
            out.append(ch)
            if ch == in_quote:
                in_quote = None
            continue
        if ch in "'\"":
            in_quote = ch
            out.append(ch)
            continue
        if ch == "#":
            break
        out.append(ch)
    return "".join(out)


def _scalar(value):
    if value in ("true", "True"):
        return True
    if value in ("false", "False"):
        return False
    if value and (value[0] == value[-1] == '"' or value[0] == value[-1] == "'") and len(value) >= 2:
        return value[1:-1]
    try:
        return int(value)
    except ValueError:
        try:
            return float(value)
        except ValueError:
            return value


def parse_yaml_subset(text):
    """Parse the narrow schema: comments, blank lines, 2-space indent,
    'key: value', 'key: [a, b]', and 'key:' mapping starts only."""
    entries = []
    for lineno, raw in enumerate(text.splitlines(), start=1):
        stripped = _strip_comment(raw).rstrip()
        if not stripped.strip():
            continue
        indent = len(stripped) - len(stripped.lstrip(" "))
        if indent % 2 != 0:
            raise DefaultsConfigError(f"line {lineno}: odd indentation is not supported: {raw!r}")
        entries.append((lineno, indent, stripped.strip()))

    root = {}
    stack = [(-1, root)]
    for lineno, indent, content in entries:
        while stack and indent <= stack[-1][0]:
            stack.pop()
        if not stack:
            raise DefaultsConfigError(f"line {lineno}: indentation does not match any open mapping")
        parent = stack[-1][1]
        if not isinstance(parent, dict):
            raise DefaultsConfigError(f"line {lineno}: list items are not supported in this schema")
        if ":" not in content:
            raise DefaultsConfigError(f"line {lineno}: expected 'key: value': {content!r}")
        key, _, value = content.partition(":")
        key = key.strip()
        value = value.strip()
        if not key:
            raise DefaultsConfigError(f"line {lineno}: empty key: {content!r}")
        if key in parent:
            raise DefaultsConfigError(f"line {lineno}: duplicate key {key!r}")
        if value == "":
            child = {}
            parent[key] = child
            stack.append((indent, child))
        elif value.startswith("[") and value.endswith("]"):
            items = [v.strip() for v in value[1:-1].split(",") if v.strip()]
            parent[key] = [_scalar(v) for v in items]
        else:
            parent[key] = _scalar(value)
    return root


def load_topology_capabilities(topology_path):
    with open(topology_path, encoding="utf-8") as f:
        data = json.load(f)
    capmap = {}
    for recipe in data.get("recipes", []):
        cap = recipe.get("capability")
        nodes = [n["id"] for n in recipe.get("standard_plus", {}).get("nodes", [])]
        capmap.setdefault(cap, set()).update(nodes)
    return capmap


def validate(config, capmap):
    """Return a list of error strings; empty list means valid."""
    errors = []
    if not isinstance(config, dict):
        return ["config root must be a mapping"]

    unknown_top = sorted(set(config) - TOP_LEVEL_KEYS)
    for key in unknown_top:
        errors.append(f"unknown top-level key: {key!r}")

    version = config.get("schema_version")
    if "schema_version" not in config:
        errors.append("missing required key: schema_version")
    elif not isinstance(version, int):
        errors.append(f"schema_version must be an integer, got {version!r}")
    elif version not in {1, 2, 3, 4}:
        errors.append(f"unsupported schema_version: {version!r}")

    enabled = []
    if version in {1, 2}:
        owner = config.get("depth1_owner")
        if not isinstance(owner, list) or not owner:
            errors.append("depth1_owner must be a non-empty list of concrete harnesses")
        else:
            seen = set()
            allowed_owners = (
                DISPATCHABLE_HARNESSES if version == 2 else LEGACY_NORMAL_HARNESSES
            )
            for h in owner:
                if not isinstance(h, str) or h not in allowed_owners:
                    errors.append(f"depth1_owner contains an unknown/non-concrete harness: {h!r}")
                elif h in seen:
                    errors.append(f"depth1_owner has a duplicate harness: {h!r}")
                seen.add(h)
            enabled = list(owner)

        opencode = config.get("opencode")
        if not isinstance(opencode, dict):
            errors.append("opencode must be a mapping")
        else:
            unknown_oc = sorted(set(opencode) - {"relief_only"})
            for key in unknown_oc:
                errors.append(f"unknown opencode key: {key!r}")
            expected_relief = version != 2
            if opencode.get("relief_only") is not expected_relief:
                errors.append(
                    "opencode.relief_only must be exactly "
                    f"{str(expected_relief).lower()} for schema v{version}"
                )
        if "harnesses" in config or "profiles" in config:
            errors.append("harnesses/profiles require schema_version 3 or 4")
    elif version in {3, 4}:
        if "depth1_owner" in config or "opencode" in config:
            errors.append("schema v3+ replaces depth1_owner/opencode with harnesses/profiles")
        harnesses = config.get("harnesses")
        if not isinstance(harnesses, dict):
            errors.append("harnesses must be a mapping for schema v3")
        else:
            for key in sorted(set(harnesses) - {"enabled"}):
                errors.append(f"unknown harnesses key: {key!r}")
            enabled = harnesses.get("enabled")
            if not isinstance(enabled, list) or not enabled:
                errors.append("harnesses.enabled must be a non-empty list")
                enabled = []
            else:
                seen = set()
                for h in enabled:
                    if h not in DISPATCHABLE_HARNESSES:
                        errors.append(f"harnesses.enabled contains unknown harness: {h!r}")
                    elif h in seen:
                        errors.append(f"harnesses.enabled has a duplicate harness: {h!r}")
                    seen.add(h)
        profiles = config.get("profiles")
        if not isinstance(profiles, dict):
            errors.append("profiles must be a mapping for schema v3")
        else:
            for name in sorted(set(profiles) - set(MODEL_PROFILES)):
                errors.append(f"unknown model profile: {name!r}")
            for name in MODEL_PROFILES:
                policy = profiles.get(name)
                if not isinstance(policy, dict):
                    errors.append(f"profiles.{name} must be a mapping")
                    continue
                allowed = set(QUALITY_BANDS) | {"promote_relief_below"}
                for key in sorted(set(policy) - allowed):
                    errors.append(f"unknown profiles.{name} key: {key!r}")
                flattened = []
                for band in QUALITY_BANDS:
                    values = policy.get(band)
                    if not isinstance(values, list):
                        errors.append(f"profiles.{name}.{band} must be a list")
                        continue
                    flattened.extend(values)
                    for h in values:
                        if h not in DISPATCHABLE_HARNESSES:
                            errors.append(f"profiles.{name}.{band} contains unknown harness: {h!r}")
                if len(flattened) != len(set(flattened)):
                    errors.append(f"profiles.{name} repeats a harness across quality bands")
                if set(flattened) != set(enabled):
                    errors.append(
                        f"profiles.{name} bands must contain every enabled harness exactly once"
                    )
                threshold = policy.get("promote_relief_below")
                if not isinstance(threshold, int) or not 0 <= threshold <= 100:
                    errors.append(
                        f"profiles.{name}.promote_relief_below must be an integer from 0 to 100"
                    )
            # AC 9 band placement gate (D8-③): OpenCode is a light-tier harness and
            # must never be placed in a deep/balanced-deep primary band; that would
            # silently push the quality-peer gate authority onto a non-quality-peer
            # family. light.primary may legitimately include opencode.
            for deep_band in ("deep", "balanced-deep"):
                band_primary = (profiles.get(deep_band) or {}).get("primary", [])
                if "opencode" in band_primary:
                    errors.append(
                        f"profiles.{deep_band}.primary must not include opencode "
                        "(quality-peer bands require claude/codex)"
                    )
                # fm M3 / anchor M2: the quality-peer set is
                # `deep.primary & balanced-deep.primary`. An empty primary band
                # validates under the coverage rule above (every enabled harness
                # still appears exactly once, just in relief/last_resort) and
                # makes that intersection empty -- a config that silently
                # nullifies the gate it is supposed to define. The gate has to
                # be defined at the layer that defines the band, not repaired at
                # each of its two consumers.
                if isinstance(band_primary, list) and not band_primary:
                    errors.append(
                        f"profiles.{deep_band}.primary must name at least one "
                        "harness (an empty band nullifies the quality-peer set)"
                    )
            # M6: closing the EMPTY band closed only one spelling of the same
            # hole. The quality-peer set is the INTERSECTION of the two bands, so
            # two non-empty but disjoint bands nullify it just as completely and
            # pass the coverage rule identically. After the AC 11 fix that is
            # strictly worse than it was: `quality_peer` is an empty set rather
            # than `None`, so `sole_gate` starts at "ok", the gated list comes out
            # empty, and every peer-bearing parallel group route-wide is refused
            # with `peer-gate:no-quality-peer-family-hard-eligible` -- a
            # config error diagnosed as a harness-availability shortage.
            deep_primary = (profiles.get("deep") or {}).get("primary")
            balanced_primary = (profiles.get("balanced-deep") or {}).get("primary")
            if (
                isinstance(deep_primary, list) and deep_primary
                and isinstance(balanced_primary, list) and balanced_primary
                and not (set(deep_primary) & set(balanced_primary))
            ):
                errors.append(
                    "profiles.deep.primary and profiles.balanced-deep.primary "
                    "must share at least one harness (their intersection is the "
                    "quality-peer set; disjoint bands nullify it)"
                )

    allocation = config.get("allocation")
    if version in {2, 3, 4}:
        if not isinstance(allocation, dict):
            errors.append(f"allocation must be a mapping for schema v{version}")
        else:
            unknown_allocation = sorted(set(allocation) - {
                "strategy", "window", "usage_gate_used_percent", "depth_affinity",
                "depth_affinity_weight", "usage_headroom_exponent",
            })
            for key in unknown_allocation:
                errors.append(f"unknown allocation key: {key!r}")
            strategy = allocation.get("strategy")
            if strategy not in ALLOCATION_STRATEGIES:
                errors.append(f"allocation.strategy must be one of {sorted(ALLOCATION_STRATEGIES)}")
            if version == 2 and strategy != "least-recent-attempts":
                errors.append("schema v2 allocation.strategy must be least-recent-attempts")
            window = allocation.get("window")
            if not isinstance(window, int) or not 3 <= window <= 300:
                errors.append("allocation.window must be an integer from 3 to 300")
            gate = allocation.get("usage_gate_used_percent", DEFAULT_USAGE_GATE_USED_PERCENT)
            if not isinstance(gate, int) or not 0 <= gate <= 100:
                errors.append("allocation.usage_gate_used_percent must be an integer from 0 to 100")
            depth_affinity = allocation.get("depth_affinity", {})
            if not isinstance(depth_affinity, dict) or not set(depth_affinity) <= {"owner", "worker"}:
                errors.append("allocation.depth_affinity keys must be a subset of owner and worker")
            elif any(value not in enabled for value in depth_affinity.values()):
                errors.append("allocation.depth_affinity values must name enabled harnesses")
            weight = allocation.get("depth_affinity_weight", 0.5)
            if isinstance(weight, bool) or not isinstance(weight, (int, float)) or not 0.0 <= weight <= 1.0:
                errors.append("allocation.depth_affinity_weight must be a number from 0.0 to 1.0")
            exponent = allocation.get("usage_headroom_exponent", 1)
            if isinstance(exponent, bool) or not isinstance(exponent, int) or not 1 <= exponent <= 4:
                errors.append("allocation.usage_headroom_exponent must be an integer from 1 to 4")
    elif allocation is not None:
        errors.append("allocation requires schema_version 2, 3, or 4")

    confirmation = config.get("confirmation")
    steward = config.get("steward")
    if version == 4:
        if confirmation is not None:
            if not isinstance(confirmation, dict):
                errors.append("confirmation must be a mapping")
            else:
                for key in sorted(set(confirmation) - {"mode"}):
                    errors.append(f"unknown confirmation key: {key!r}")
                mode = confirmation.get("mode", DEFAULT_CONFIRMATION_MODE)
                if mode not in CONFIRMATION_MODES:
                    errors.append(
                        f"confirmation.mode must be one of {sorted(CONFIRMATION_MODES)}"
                    )
        if steward is not None:
            if not isinstance(steward, dict):
                errors.append("steward must be a mapping")
            else:
                for key in sorted(set(steward) - {"child_permission_mode"}):
                    errors.append(f"unknown steward key: {key!r}")
                mode = steward.get("child_permission_mode", DEFAULT_STEWARD_CHILD_PERMISSION_MODE)
                if mode not in STEWARD_CHILD_PERMISSION_MODES:
                    errors.append(
                        "steward.child_permission_mode must be one of "
                        f"{sorted(STEWARD_CHILD_PERMISSION_MODES)}"
                    )
    else:
        if confirmation is not None:
            errors.append("confirmation requires schema_version 4")
        if steward is not None:
            errors.append("steward requires schema_version 4")

    headless = config.get("headless")
    if headless is not None:
        if not isinstance(headless, dict):
            errors.append("headless must be a mapping")
        else:
            for key in sorted(set(headless) - HEADLESS_KEYS):
                errors.append(f"unknown headless key: {key!r}")
            mode = headless.get("claude_permission_mode", DEFAULT_HEADLESS_PERMISSION_MODE)
            if mode not in HEADLESS_PERMISSION_MODES:
                errors.append(
                    "headless.claude_permission_mode must be one of "
                    f"{list(HEADLESS_PERMISSION_MODES)}, got {mode!r}"
                )

    caps = config.get("capabilities", {})
    if not isinstance(caps, dict):
        errors.append("capabilities must be a mapping")
    else:
        for cap_name, stagemap in caps.items():
            if cap_name not in capmap:
                errors.append(f"unknown capability: {cap_name!r}")
                continue
            if not isinstance(stagemap, dict):
                errors.append(f"capabilities.{cap_name} must be a mapping of stage -> affinity")
                continue
            for stage_name, value in stagemap.items():
                if stage_name not in capmap[cap_name]:
                    errors.append(
                        f"unknown stage {stage_name!r} for capability {cap_name!r} "
                        f"(canonical node ids: {sorted(capmap[cap_name])})"
                    )
                    continue
                if value not in AFFINITY_VALUES:
                    errors.append(
                        f"invalid affinity value for {cap_name}.{stage_name}: {value!r} "
                        f"(allowed: {sorted(AFFINITY_VALUES)}; model/effort values are never allowed here)"
                    )
                elif version in {3, 4} and value != "diverse" and value not in enabled:
                    errors.append(
                        f"affinity {cap_name}.{stage_name} targets disabled harness: {value!r}"
                    )
    return errors


def load_and_validate(config_path, topology_path):
    with open(config_path, encoding="utf-8") as f:
        text = f.read()
    config = parse_yaml_subset(text)
    capmap = load_topology_capabilities(topology_path)
    errors = validate(config, capmap)
    if errors:
        raise DefaultsConfigError("; ".join(errors))
    # Merging before validation would turn a valid user file into a loud
    # failure for every user the moment the shipped baseline goes stale, so
    # this runs strictly after validation of the raw user config above. The
    # shipped file merging into itself would be a no-op anyway, but the path
    # comparison also avoids a redundant re-parse.
    if os.path.realpath(config_path) == os.path.realpath(SHIPPED_CONFIG_PATH):
        return config
    try:
        baseline = shipped_capability_baseline()
    except (DefaultsConfigError, OSError, json.JSONDecodeError) as exc:
        raise DefaultsConfigError(
            f"corrupt shipped dispatch-defaults baseline: {exc}"
        ) from exc
    return merge_capability_baseline(config, capmap, baseline=baseline)


def query_affinity(config, capability, stage):
    if not capability or not stage:
        return "neutral"
    caps = config.get("capabilities", {})
    stagemap = caps.get(capability)
    if not isinstance(stagemap, dict):
        return "neutral"
    value = stagemap.get(stage)
    if value not in AFFINITY_VALUES:
        return "neutral"
    return value


def query_stage_affinity(config, capability, stage):
    """SD-68 record-seal vocabulary: like query_affinity but a missing/unknown
    cell maps to 'unspecified' (the record-seal word), never 'neutral' (the
    selector word). Vocabulary ownership stays in this loader module."""
    value = query_affinity(config, capability, stage)
    return value if value in AFFINITY_VALUES else "unspecified"


def query_owners(config):
    if config.get("schema_version") in {3, 4}:
        return list((config.get("harnesses") or {}).get("enabled", []))
    return config.get("depth1_owner", [])


def query_profile_policy(config, profile):
    """Return ordered quality bands and the relief-promotion threshold.

    Legacy schemas have one symmetric primary band.  This preserves their exact
    selector behavior while letting v3 keep OpenCode outside the quality-peer set.
    """
    if profile not in MODEL_PROFILES:
        raise DefaultsConfigError(f"unknown model profile: {profile!r}")
    if config.get("schema_version") not in {3, 4}:
        return {
            "primary": list(query_owners(config)),
            "relief": [],
            "last_resort": [],
            "promote_relief_below": 0,
        }
    policy = config["profiles"][profile]
    result = {
        band: list(policy[band]) for band in QUALITY_BANDS
    }
    result["promote_relief_below"] = policy["promote_relief_below"]
    return result


def query_allocation(config):
    neutral = {"depth_affinity": {}, "depth_affinity_weight": 0.5, "usage_headroom_exponent": 1}
    allocation = config.get("allocation")
    if config.get("schema_version") in {2, 3, 4} and isinstance(allocation, dict):
        result = {
            "strategy": allocation["strategy"],
            "window": allocation["window"],
            "usage_gate_used_percent": allocation.get("usage_gate_used_percent", DEFAULT_USAGE_GATE_USED_PERCENT),
            "harness_order": list(query_owners(config)),
        }
        result.update({key: allocation.get(key, value) for key, value in neutral.items()})
        return result
    result = {
        "strategy": "config-order",
        "window": 0,
        "usage_gate_used_percent": DEFAULT_USAGE_GATE_USED_PERCENT,
        "harness_order": list(query_owners(config)),
    }
    result.update(neutral)
    return result


def query_headless_policy(config):
    """Return the registered-headless permission posture the config selects.

    `source` says whether the value came from the file (`config`) or from the
    shipped default because the section/key is absent (`shipped-default`), so a
    wrapper receipt can name where the applied posture came from.
    """
    headless = config.get("headless")
    if isinstance(headless, dict) and "claude_permission_mode" in headless:
        return {
            "claude_permission_mode": headless["claude_permission_mode"],
            "source": "config",
        }
    return {
        "claude_permission_mode": DEFAULT_HEADLESS_PERMISSION_MODE,
        "source": "shipped-default",
    }


def query_opencode_policy(config):
    if config.get("schema_version") in {3, 4}:
        policies = [query_profile_policy(config, profile) for profile in MODEL_PROFILES]
        if all("opencode" not in policy["primary"] for policy in policies):
            return "quality-banded"
        return "normal"
    opencode = config.get("opencode", {})
    if isinstance(opencode, dict) and opencode.get("relief_only") is True:
        return "relief-only"
    if isinstance(opencode, dict) and opencode.get("relief_only") is False:
        return "normal"
    return "unknown"


def query_confirmation_mode(config):
    """SD-123: post-frame direction-gate confirmation mode.

    Returns the default (`hybrid`) whenever the `confirmation` block or its
    `mode` key is absent — including for a schema_version < 4 document,
    where the block is not legal at all — so every caller gets a value
    without special-casing older configs.
    """
    confirmation = config.get("confirmation")
    if isinstance(confirmation, dict):
        mode = confirmation.get("mode")
        if mode in CONFIRMATION_MODES:
            return mode
    return DEFAULT_CONFIRMATION_MODE


def query_steward_child_permission_mode(config):
    """SD-122 (9): default child-session permission mode for `peer-steward.py
    start`. Returns `bypass` whenever the `steward` block or its
    `child_permission_mode` key is absent, including for schema_version < 4.
    """
    steward = config.get("steward")
    if isinstance(steward, dict):
        mode = steward.get("child_permission_mode")
        if mode in STEWARD_CHILD_PERMISSION_MODES:
            return mode
    return DEFAULT_STEWARD_CHILD_PERMISSION_MODE


def shipped_allocation_strategy():
    """Strategy the shipped profiles/dispatch-defaults.yaml declares, or None."""
    try:
        with open(SHIPPED_CONFIG_PATH, encoding="utf-8") as f:
            shipped = parse_yaml_subset(f.read())
    except (OSError, DefaultsConfigError):
        return None
    allocation = shipped.get("allocation")
    return allocation.get("strategy") if isinstance(allocation, dict) else None


def allocation_warnings(config, config_path=None):
    """Non-fatal drift findings for an already-valid allocation block.

    Two shapes are reported, both of which validated silently before
    2026-08-29:
    - the user-owned file runs a strategy other than the shipped default, so a
      decision recorded in the template never reached this host (DP-23 keeps
      the file untouched by design — the report is the substitute);
    - an optional key is present but the configured strategy never reads it
      (or only honors a degraded form of it), so the key is inert.
    """
    allocation = config.get("allocation")
    if not isinstance(allocation, dict):
        return []
    warnings = []
    strategy = allocation.get("strategy")
    shipped = shipped_allocation_strategy()
    is_shipped_file = (
        config_path is not None
        and os.path.realpath(config_path) == os.path.realpath(SHIPPED_CONFIG_PATH)
    )
    if shipped and strategy and strategy != shipped and not is_shipped_file:
        warnings.append(
            f"allocation.strategy={strategy} differs from shipped default {shipped}"
            " (user-owned file is never overwritten; edit it to adopt the decision)"
        )
    for key, why in inert_allocation_keys(allocation).items():
        warnings.append(f"allocation.{key} is inert: {why}")
    return warnings


def _arg(args, flag, default=None):
    if flag in args:
        i = args.index(flag)
        if i + 1 >= len(args):
            raise DefaultsConfigError(f"{flag} requires a value")
        return args[i + 1]
    return default


def main(argv):
    if not argv:
        print("usage: dispatch-defaults.py <validate|affinity|owners|policy|allocation|opencode-policy|confirmation-mode> [options]", file=sys.stderr)
        return 64

    op = argv[0]
    rest = argv[1:]
    config_path = _arg(rest, "--config", default_config_path())
    topology_path = _arg(rest, "--topology", default_topology_path())

    try:
        config = load_and_validate(config_path, topology_path)
    except (DefaultsConfigError, OSError, json.JSONDecodeError) as exc:
        print(f"dispatch-defaults: invalid config {config_path}: {exc}", file=sys.stderr)
        return 65

    if op == "validate":
        print(f"dispatch-defaults: {config_path} is valid")
        # Warnings never change the exit code: a valid file stays valid. They
        # exist because "valid" alone hid two weeks of a balanced-first decision
        # that never reached the user-owned file (2026-08-13 -> 2026-08-29).
        for warning in allocation_warnings(config, config_path):
            print(f"warning={warning}")
        return 0
    if op == "affinity":
        capability = _arg(rest, "--capability", "")
        stage = _arg(rest, "--stage", "")
        print(query_affinity(config, capability, stage))
        return 0
    if op == "owners":
        print(",".join(query_owners(config)))
        return 0
    if op == "policy":
        policy = query_profile_policy(config, _arg(rest, "--profile", "light"))
        print(json.dumps(policy, sort_keys=True))
        return 0
    if op == "allocation":
        allocation = query_allocation(config)
        print(f"strategy={allocation['strategy']}")
        print(f"window={allocation['window']}")
        print(f"usage_gate_used_percent={allocation['usage_gate_used_percent']}")
        print("depth_affinity=" + ",".join(f"{key}:{allocation['depth_affinity'][key]}" for key in sorted(allocation["depth_affinity"])))
        print(f"depth_affinity_weight={allocation['depth_affinity_weight']}")
        print(f"usage_headroom_exponent={allocation['usage_headroom_exponent']}")
        print("harness_order=" + ",".join(allocation["harness_order"]))
        return 0
    if op == "opencode-policy":
        print(query_opencode_policy(config))
        return 0
    if op == "confirmation-mode":
        print(query_confirmation_mode(config))
        return 0

    print(f"dispatch-defaults: unknown operation {op!r}", file=sys.stderr)
    return 64


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
