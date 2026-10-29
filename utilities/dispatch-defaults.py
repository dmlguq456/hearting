#!/usr/bin/env python3
"""Strict standard-library loader/validator/query CLI for dispatch defaults.

Parses a deliberately narrow YAML subset (scalars, inline lists, nested
mappings, comments) — no PyYAML/yq dependency. Validates against the
canonical topology node ids in capabilities/topologies.json and exposes
affinity/owner/allocation/quality-band queries.  Schemas v1/v2 remain readable;
schema v3 adds user-local enabled harnesses and per-profile quality bands;
schema v4 adds confirmation/steward policy and optional `worker_account` auth-source paths.
"""
import copy
import json
import math
import os
import sys

LEGACY_NORMAL_HARNESSES = {"claude", "codex"}
DISPATCHABLE_HARNESSES = {"claude", "codex", "opencode"}
KNOWN_HARNESSES = DISPATCHABLE_HARNESSES
AFFINITY_VALUES = {"claude", "codex", "opencode", "diverse"}
MODEL_PROFILES = ("deep", "balanced-deep", "balanced", "light", "mini")
# The `top` exception profile never has its own policy section: a route that
# seals it borrows the quality-peer bands of `deep` (claude/codex only). A
# `profiles.top` mapping in the user file stays an unknown-profile error.
EXCEPTION_PROFILE_POLICY = {"top": "deep"}
QUALITY_BANDS = ("primary", "relief", "last_resort")
ALLOCATION_STRATEGIES = {"least-recent-attempts", "capacity-aware", "balanced"}
DEFAULT_USAGE_GATE_USED_PERCENT = 90
TOP_LEVEL_KEYS = {
    "schema_version", "depth1_owner", "opencode", "allocation", "capabilities",
    "harnesses", "profiles", "headless", "confirmation", "steward", "runtime",
    "worker_account",
}
# PRD §13.53.2: the operator kill switch for the SD-120/121 terminal fast
# path. `auto` lets the sealed runtime-capability census decide (the default,
# and the only value that can ever open the gate); `off` keeps every route on
# the legacy owner-driven close/finalize path regardless of what the runtime
# publishes. This is deliberately *not* an on/true value: nothing an operator
# writes here can force activation on a runtime that lacks the contract.
# This module *validates* the key but deliberately does not read it: the one
# reader is `dispatch_runtime_support.query_support_mode`. A second reader here
# under the idiomatic `query_*` name is how a later caller silently gets the
# opposite failure direction -- that reader fails closed on an unrecognised
# value, and a `query_*` twin that fell back to `auto` would fail open.
RUNTIME_KEYS = {"terminal_commit"}
TERMINAL_COMMIT_SUPPORT_MODES = ("auto", "off")
DEFAULT_TERMINAL_COMMIT_SUPPORT = "auto"
# core/OPERATIONS.md §5.10 "Registered headless permission posture": the
# Claude wrapper pins the starting permission mode of every registered
# `claude -p` turn. `bypass` appends `--permission-mode bypassPermissions`;
# `allowlist` keeps the runtime's starting mode and pre-approves only the
# harness utilities. The shipped default is `bypass`; omitting the section
# selects it.
HEADLESS_KEYS = {"claude_permission_mode", "no_progress_minutes"}
HEADLESS_PERMISSION_MODES = ("bypass", "allowlist")
DEFAULT_HEADLESS_PERMISSION_MODE = "bypass"
DEFAULT_NO_PROGRESS_MINUTES = 30.0
MAX_NO_PROGRESS_MINUTES = 1440.0
CONFIRMATION_MODES = ("hybrid", "both", "post-frame-only")
DEFAULT_CONFIRMATION_MODE = "hybrid"
# SD-136: how a `direct`/`solo`(quick) route is confirmed before material work.
# `notice` replaces the blocking five-field card with one `[경로]` line (the
# route is atomic or one bounded owner and reversible); `card` keeps the
# blocking §0.4 card. Destructive or external-facing work keeps the card under
# either value -- the route seals the value, WORKFLOW §0.4 owns the rule.
SMALL_WORK_CONFIRMATION_MODES = ("notice", "card")
DEFAULT_SMALL_WORK_CONFIRMATION = "notice"
STEWARD_CHILD_PERMISSION_MODES = ("bypass", "inherit")
DEFAULT_STEWARD_CHILD_PERMISSION_MODE = "bypass"
STEWARD_KEYS = {"child_permission_mode", "check_interval_seconds", "max_supervisors_per_repo"}
DEFAULT_STEWARD_CHECK_INTERVAL_SECONDS = 7200
STEWARD_CHECK_INTERVAL_RANGE = (600, 86400)  # inclusive seconds
DEFAULT_STEWARD_MAX_SUPERVISORS_PER_REPO = 2
STEWARD_MAX_SUPERVISORS_RANGE = (1, 8)  # inclusive


class DefaultsConfigError(Exception):
    pass


sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
from dispatch_allocation import inert_allocation_keys  # noqa: E402


def _int_in_range(value, bounds):
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and bounds[0] <= value <= bounds[1]
    )


def _repo_root():
    # realpath, not abspath: the helper is projected into
    # adapters/<harness>/utilities/ as a symlink, and the shipped config and
    # topology registry live only at the real repo root.
    return os.path.dirname(os.path.dirname(os.path.realpath(__file__)))


def default_config_path(environ=None):
    """Resolve the user config from the supplied environment when provided."""
    environment = os.environ if environ is None else environ
    override = environment.get("DISPATCH_DEFAULTS_CONFIG")
    if override:
        return override
    config_home = environment.get("XDG_CONFIG_HOME")
    if not config_home:
        home = environment.get("HOME")
        if home:
            config_home = os.path.join(home, ".config")
        elif environ is None:
            config_home = os.path.join(os.path.expanduser("~"), ".config")
    if config_home:
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
            # `balanced` is the SD-88 extension. Old v3/v4 user files are
            # complete policies without it and inherit their light policy at
            # read time; all pre-extension profiles remain required.
            for name in MODEL_PROFILES:
                if name == "balanced" and name not in profiles:
                    continue
                policy = profiles.get(name)
                if not isinstance(policy, dict):
                    errors.append(f"profiles.{name} must be a mapping")
                    continue
                allowed = set(QUALITY_BANDS) | {"promote_relief_below"}
                for key in sorted(set(policy) - allowed):
                    errors.append(f"unknown profiles.{name} key: {key!r}")
                for band in QUALITY_BANDS:
                    values = policy.get(band)
                    if not isinstance(values, list):
                        errors.append(f"profiles.{name}.{band} must be a list")
                        continue
                    for h in values:
                        if h not in DISPATCHABLE_HARNESSES:
                            errors.append(f"profiles.{name}.{band} contains unknown harness: {h!r}")
                threshold = policy.get("promote_relief_below")
                if not isinstance(threshold, int) or not 0 <= threshold <= 100:
                    errors.append(
                        f"profiles.{name}.promote_relief_below must be an integer from 0 to 100"
                    )
            # Band placement (a band naming a disabled harness, one harness in
            # several bands, OpenCode in a deep primary band, an empty or
            # disjoint quality-peer pair, an unused enabled harness) is a
            # recommendation, not a structural error: `policy_warnings` reports
            # it and `normalize_policy` reads the file as the user wrote it.

    if "worker_account" in config:
        accounts = config.get("worker_account")
        if version != 4:
            errors.append("worker_account requires schema_version 4")
        if not isinstance(accounts, dict):
            errors.append("worker_account must be a mapping")
        else:
            for harness in sorted(set(accounts) - DISPATCHABLE_HARNESSES):
                errors.append(f"worker_account names an unknown harness: {harness!r}")
            for harness, path in accounts.items():
                if harness in DISPATCHABLE_HARNESSES and (
                    not isinstance(path, str) or not path.strip() or "\0" in path
                ):
                    errors.append(
                        f"worker_account.{harness} must be a non-empty path string"
                    )

    allocation = config.get("allocation")
    if version in {2, 3, 4}:
        if not isinstance(allocation, dict):
            errors.append(f"allocation must be a mapping for schema v{version}")
        else:
            unknown_allocation = sorted(set(allocation) - {
                "strategy", "window", "usage_gate_used_percent", "depth_affinity",
                "depth_affinity_weight", "usage_headroom_exponent", "harness_weights", "owner_order",
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
            elif any(value not in DISPATCHABLE_HARNESSES for value in depth_affinity.values()):
                errors.append("allocation.depth_affinity values must name known harnesses")
            if "owner_order" in allocation and not valid_owner_order(allocation["owner_order"]):
                errors.append("allocation.owner_order must be a list of distinct known harnesses")
            if version == 2 and "owner_order" in allocation:
                errors.append("allocation.owner_order requires schema_version 3 or 4")
            weight = allocation.get("depth_affinity_weight", 0.5)
            if isinstance(weight, bool) or not isinstance(weight, (int, float)) or not 0.0 <= weight <= 1.0:
                errors.append("allocation.depth_affinity_weight must be a number from 0.0 to 1.0")
            exponent = allocation.get("usage_headroom_exponent", 1)
            if isinstance(exponent, bool) or not isinstance(exponent, int) or not 1 <= exponent <= 4:
                errors.append("allocation.usage_headroom_exponent must be an integer from 1 to 4")
            if "harness_weights" in allocation:
                weights = allocation["harness_weights"]
                if not isinstance(weights, dict):
                    errors.append(
                        "allocation.harness_weights must be a block mapping of harness -> weight "
                        "(inline {a: 1} mappings are not supported by this parser)"
                    )
                else:
                    for name, value in weights.items():
                        if name not in DISPATCHABLE_HARNESSES:
                            errors.append(f"allocation.harness_weights names an unknown harness: {name!r}")
                        elif isinstance(value, bool) or not isinstance(value, (int, float)) or not 0.0 < value <= 1.0:
                            errors.append(
                                f"allocation.harness_weights.{name} must be a number above 0 and at most 1"
                            )
    elif allocation is not None:
        errors.append("allocation requires schema_version 2, 3, or 4")

    confirmation = config.get("confirmation")
    steward = config.get("steward")
    runtime = config.get("runtime")
    if version == 4:
        if confirmation is not None:
            if not isinstance(confirmation, dict):
                errors.append("confirmation must be a mapping")
            else:
                for key in sorted(set(confirmation) - {"mode", "small_work"}):
                    errors.append(f"unknown confirmation key: {key!r}")
                mode = confirmation.get("mode", DEFAULT_CONFIRMATION_MODE)
                if mode not in CONFIRMATION_MODES:
                    errors.append(
                        f"confirmation.mode must be one of {sorted(CONFIRMATION_MODES)}"
                    )
                small = confirmation.get("small_work", DEFAULT_SMALL_WORK_CONFIRMATION)
                if small not in SMALL_WORK_CONFIRMATION_MODES:
                    errors.append(
                        "confirmation.small_work must be one of "
                        f"{sorted(SMALL_WORK_CONFIRMATION_MODES)}"
                    )
        if steward is not None:
            if not isinstance(steward, dict):
                errors.append("steward must be a mapping")
            else:
                for key in sorted(set(steward) - STEWARD_KEYS):
                    errors.append(f"unknown steward key: {key!r}")
                mode = steward.get("child_permission_mode", DEFAULT_STEWARD_CHILD_PERMISSION_MODE)
                if mode not in STEWARD_CHILD_PERMISSION_MODES:
                    errors.append(
                        "steward.child_permission_mode must be one of "
                        f"{sorted(STEWARD_CHILD_PERMISSION_MODES)}"
                    )
                for key, bounds in (
                    ("check_interval_seconds", STEWARD_CHECK_INTERVAL_RANGE),
                    ("max_supervisors_per_repo", STEWARD_MAX_SUPERVISORS_RANGE),
                ):
                    if key in steward and not _int_in_range(steward[key], bounds):
                        errors.append(
                            f"steward.{key} must be an integer from {bounds[0]} to {bounds[1]}"
                        )
        if runtime is not None:
            if not isinstance(runtime, dict):
                errors.append("runtime must be a mapping")
            else:
                for key in sorted(set(runtime) - RUNTIME_KEYS):
                    errors.append(f"unknown runtime key: {key!r}")
                mode = runtime.get("terminal_commit", DEFAULT_TERMINAL_COMMIT_SUPPORT)
                # `False` is accepted because YAML 1.1 resolves a bare `off`
                # to boolean false; the reader treats both as disabled.
                if mode not in TERMINAL_COMMIT_SUPPORT_MODES and mode is not False:
                    errors.append(
                        "runtime.terminal_commit must be one of "
                        f"{sorted(TERMINAL_COMMIT_SUPPORT_MODES)}"
                    )
    else:
        if confirmation is not None:
            errors.append("confirmation requires schema_version 4")
        if steward is not None:
            errors.append("steward requires schema_version 4")
        if runtime is not None:
            errors.append("runtime requires schema_version 4")

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
    return errors


def _enabled_harnesses(config):
    """Enabled harness names for any schema version (empty when unreadable)."""
    enabled = query_owners(config)
    return list(enabled) if isinstance(enabled, list) else []


def _band_lists(policy):
    return {
        band: list(policy.get(band))
        for band in QUALITY_BANDS
        if isinstance(policy.get(band), list)
    }


def policy_warnings(config):
    """Recommendation findings for a structurally valid config.

    None of these reject the file: `normalize_policy` reads the file the way
    the warnings describe and the caller prints each line as `warning=<text>`.
    W1 band names a disabled harness (ignored) · W2 one harness in several
    bands (first band wins) · W3 OpenCode in a deep/balanced-deep primary band
    · W4 deep/balanced-deep primary empty or disjoint (no quality-peer family)
    · W5 a profile holds no enabled harness (all enabled harnesses become its
    last resort) · W6 affinity, depth_affinity or harness_weights name a
    disabled harness (that cell is ignored). A harness absent from a profile is
    a normal "this profile does not use it", never a finding. W3/W4 only apply
    while claude or codex is enabled: an OpenCode-only user has no
    recommendation left to break.
    """
    warnings = []
    headless = config.get("headless")
    if isinstance(headless, dict) and "no_progress_minutes" in headless:
        _minutes, warning = normalize_no_progress_minutes(headless["no_progress_minutes"])
        if warning:
            warnings.append(warning)
    version = config.get("schema_version")
    enabled = _enabled_harnesses(config)
    profiles = config.get("profiles")
    if version in {3, 4} and isinstance(profiles, dict):
        for name in MODEL_PROFILES:
            policy = profiles.get(name)
            if not isinstance(policy, dict):
                continue
            seen = {}
            for band, values in _band_lists(policy).items():
                for h in values:
                    if h not in enabled:
                        warnings.append(
                            f"profiles.{name}.{band} names disabled harness {h!r}: ignored"
                        )
                    elif h in seen:
                        warnings.append(
                            f"profiles.{name} repeats {h!r} in {seen[h]} and {band}: "
                            f"keeping {seen[h]}"
                        )
                    else:
                        seen[h] = band
            if enabled and not seen:
                warnings.append(
                    f"profiles.{name} holds no enabled harness: "
                    "every enabled harness becomes its last_resort"
                )
        if {"claude", "codex"} & set(enabled):
            for deep_band in ("deep", "balanced-deep"):
                primary = (profiles.get(deep_band) or {}).get("primary")
                if isinstance(primary, list) and "opencode" in primary:
                    warnings.append(
                        f"profiles.{deep_band}.primary includes opencode "
                        "(quality-peer bands are recommended to be claude/codex)"
                    )
            deep_primary = (profiles.get("deep") or {}).get("primary")
            balanced_primary = (profiles.get("balanced-deep") or {}).get("primary")
            if isinstance(deep_primary, list) and isinstance(balanced_primary, list):
                if not deep_primary or not balanced_primary:
                    warnings.append(
                        "profiles.deep.primary / profiles.balanced-deep.primary is empty: "
                        "no quality-peer family (a parallel peer gate is recorded as "
                        "sole-gate-non-peer-harness instead of refused)"
                    )
                elif not (set(deep_primary) & set(balanced_primary)):
                    warnings.append(
                        "profiles.deep.primary and profiles.balanced-deep.primary share no "
                        "harness: no quality-peer family (a parallel peer gate is recorded as "
                        "sole-gate-non-peer-harness instead of refused)"
                    )
        capabilities = config.get("capabilities")
        if isinstance(capabilities, dict):
            for cap_name, stagemap in capabilities.items():
                if not isinstance(stagemap, dict):
                    continue
                for stage_name, value in stagemap.items():
                    if value in DISPATCHABLE_HARNESSES and value not in enabled:
                        warnings.append(
                            f"affinity {cap_name}.{stage_name} targets disabled harness "
                            f"{value!r}: ignored"
                        )
    allocation = config.get("allocation")
    if isinstance(allocation, dict):
        for name in allocation.get("owner_order", []):
            if name not in enabled:
                warnings.append(f"allocation.owner_order names disabled harness {name!r}: ignored")
        depth_affinity = allocation.get("depth_affinity")
        if isinstance(depth_affinity, dict):
            for key, value in depth_affinity.items():
                if value in DISPATCHABLE_HARNESSES and value not in enabled:
                    warnings.append(
                        f"allocation.depth_affinity.{key} names disabled harness {value!r}: ignored"
                    )
        weights = allocation.get("harness_weights")
        if isinstance(weights, dict):
            for name in weights:
                if name in DISPATCHABLE_HARNESSES and name not in enabled:
                    warnings.append(
                        f"allocation.harness_weights.{name} names a disabled harness: ignored"
                    )
    return warnings


def normalize_policy(config):
    """Return the policy the readers should use: recommendation findings applied.

    Pure function over a structurally valid config (see `policy_warnings` for
    W1/W2/W5/W6). W3/W4 change nothing here. The input is not mutated.
    """
    result = copy.deepcopy(config)
    headless = result.get("headless")
    if isinstance(headless, dict) and "no_progress_minutes" in headless:
        headless["no_progress_minutes"] = normalize_no_progress_minutes(
            headless["no_progress_minutes"]
        )[0]
    version = result.get("schema_version")
    enabled = _enabled_harnesses(result)
    profiles = result.get("profiles")
    if version in {3, 4} and isinstance(profiles, dict):
        for name in MODEL_PROFILES:
            policy = profiles.get(name)
            if not isinstance(policy, dict):
                continue
            placed = set()
            for band in QUALITY_BANDS:
                values = policy.get(band)
                if not isinstance(values, list):
                    continue
                kept = []
                for h in values:
                    if h in enabled and h not in placed:
                        kept.append(h)
                        placed.add(h)
                policy[band] = kept
            if enabled and not placed:
                policy["last_resort"] = list(enabled)
        capabilities = result.get("capabilities")
        if isinstance(capabilities, dict):
            for stagemap in capabilities.values():
                if not isinstance(stagemap, dict):
                    continue
                for stage_name in [
                    key for key, value in stagemap.items()
                    if value in DISPATCHABLE_HARNESSES and value not in enabled
                ]:
                    del stagemap[stage_name]
    allocation = result.get("allocation")
    if isinstance(allocation, dict):
        if "owner_order" in allocation:
            allocation["owner_order"] = [h for h in allocation["owner_order"] if h in enabled]
        depth_affinity = allocation.get("depth_affinity")
        if isinstance(depth_affinity, dict):
            for key in [k for k, v in depth_affinity.items() if v not in enabled]:
                del depth_affinity[key]
        weights = allocation.get("harness_weights")
        if isinstance(weights, dict):
            for name in [n for n in weights if n not in enabled]:
                del weights[name]
            if not weights:
                del allocation["harness_weights"]
    return result


def load_validated(config_path, topology_path):
    """Load, validate structure, normalize, and return `(config, warnings)`.

    Structural errors raise `DefaultsConfigError`; recommendation findings come
    back as warning strings and the returned config is already normalized.
    """
    with open(config_path, encoding="utf-8") as f:
        text = f.read()
    config = parse_yaml_subset(text)
    capmap = load_topology_capabilities(topology_path)
    errors = validate(config, capmap)
    if errors:
        raise DefaultsConfigError("; ".join(errors))
    warnings = policy_warnings(config)
    config = normalize_policy(config)
    # Merging before validation would turn a valid user file into a loud
    # failure for every user the moment the shipped baseline goes stale, so
    # this runs strictly after validation of the raw user config above. The
    # shipped file merging into itself would be a no-op anyway, but the path
    # comparison also avoids a redundant re-parse.
    if os.path.realpath(config_path) == os.path.realpath(SHIPPED_CONFIG_PATH):
        return config, warnings
    try:
        baseline = shipped_capability_baseline()
    except (DefaultsConfigError, OSError, json.JSONDecodeError) as exc:
        raise DefaultsConfigError(
            f"corrupt shipped dispatch-defaults baseline: {exc}"
        ) from exc
    return merge_capability_baseline(config, capmap, baseline=baseline), warnings


def load_and_validate(config_path, topology_path):
    return load_validated(config_path, topology_path)[0]


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


def current_enabled_harnesses(config=None):
    """Current automatic selection ceiling, separate from prior exact admission.

    Call only for new choices/launches, never for historical evidence readers.
    Legacy schemas have no disabled-harness declaration: their owner ordering
    and fallback meanings remain with the existing selectors.
    """
    if config is None:
        try:
            config = load_and_validate(default_config_path(), default_topology_path())
        except (OSError, ValueError) as exc:
            raise DefaultsConfigError(f"defaults-invalid:{exc}") from exc
    if config.get("schema_version") in {3, 4}:
        return tuple(query_owners(config))
    return tuple(sorted(DISPATCHABLE_HARNESSES))


def restrict_harness_policy(policy, harnesses):
    """Intersect existing bands without normalizing or filling an empty band."""
    allowed = set(harnesses)
    return {**policy, **{band: [h for h in policy.get(band, []) if h in allowed]
                         for band in QUALITY_BANDS}}


def query_worker_account(config, harness):
    """Return the optional authentication-source path for one harness."""
    if harness not in DISPATCHABLE_HARNESSES or config.get("schema_version") != 4:
        return None
    accounts = config.get("worker_account")
    if not isinstance(accounts, dict):
        return None
    path = accounts.get(harness)
    return path if isinstance(path, str) and path.strip() else None


def query_profile_policy(config, profile):
    """Return ordered quality bands and the relief-promotion threshold.

    Legacy schemas have one symmetric primary band.  This preserves their exact
    selector behavior while letting v3 keep OpenCode outside the quality-peer set.
    """
    profile = EXCEPTION_PROFILE_POLICY.get(profile, profile)
    if profile not in MODEL_PROFILES:
        raise DefaultsConfigError(f"unknown model profile: {profile!r}")
    if config.get("schema_version") not in {3, 4}:
        return {
            "primary": list(query_owners(config)),
            "relief": [],
            "last_resort": [],
            "promote_relief_below": 0,
        }
    policy = config["profiles"].get(profile)
    if profile == "balanced" and profile not in config["profiles"]:
        light = config["profiles"].get("light")
        if not isinstance(light, dict):
            raise DefaultsConfigError("profiles.light is required to derive balanced")
        policy = dict(light)
    if not isinstance(policy, dict):
        raise DefaultsConfigError(f"profiles.{profile} must be a mapping")
    result = {
        band: list(policy[band]) for band in QUALITY_BANDS
    }
    result["promote_relief_below"] = policy["promote_relief_below"]
    return result


def valid_owner_order(value):
    return (isinstance(value, list)
            and all(isinstance(h, str) and h in DISPATCHABLE_HARNESSES for h in value)
            and len(set(value)) == len(value))


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
        # Optional and absent by default, so a config without weights seals
        # the same allocation bytes as before this key existed.
        if isinstance(allocation.get("harness_weights"), dict):
            result["harness_weights"] = dict(allocation["harness_weights"])
        if "owner_order" in allocation:
            result["owner_order"] = list(allocation["owner_order"])
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


def normalize_no_progress_minutes(raw):
    """Normalize the optional launcher activity window and explain corrections."""
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or not math.isfinite(raw):
        return DEFAULT_NO_PROGRESS_MINUTES, (
            f"headless.no_progress_minutes is invalid ({raw!r}); using "
            f"{DEFAULT_NO_PROGRESS_MINUTES:g}"
        )
    value = float(raw)
    bounded = min(MAX_NO_PROGRESS_MINUTES, max(0.0, value))
    if bounded != value:
        return bounded, (
            f"headless.no_progress_minutes {value:g} is outside 0–"
            f"{MAX_NO_PROGRESS_MINUTES:g}; clamped to {bounded:g}"
        )
    return bounded, None


def query_no_progress_minutes(config):
    """Return the optional launcher activity window in minutes."""
    headless = config.get("headless")
    raw = headless.get("no_progress_minutes", DEFAULT_NO_PROGRESS_MINUTES) if isinstance(headless, dict) else DEFAULT_NO_PROGRESS_MINUTES
    return normalize_no_progress_minutes(raw)[0]


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


def query_small_work_confirmation(config):
    """SD-136: `direct`/`solo` confirmation surface. Returns the default
    (`notice`) whenever the `confirmation` block or its `small_work` key is
    absent, including for schema_version < 4."""
    confirmation = config.get("confirmation")
    if isinstance(confirmation, dict):
        mode = confirmation.get("small_work")
        if mode in SMALL_WORK_CONFIRMATION_MODES:
            return mode
    return DEFAULT_SMALL_WORK_CONFIRMATION


def query_steward_policy(config):
    """Steward settings with every absent or invalid key at its shipped
    default (schema_version < 4 included):
    `{"child_permission_mode", "check_interval_seconds",
    "max_supervisors_per_repo"}`."""
    steward = config.get("steward")
    if not isinstance(steward, dict):
        steward = {}
    mode = steward.get("child_permission_mode")
    if mode not in STEWARD_CHILD_PERMISSION_MODES:
        mode = DEFAULT_STEWARD_CHILD_PERMISSION_MODE
    interval = steward.get("check_interval_seconds")
    if not _int_in_range(interval, STEWARD_CHECK_INTERVAL_RANGE):
        interval = DEFAULT_STEWARD_CHECK_INTERVAL_SECONDS
    limit = steward.get("max_supervisors_per_repo")
    if not _int_in_range(limit, STEWARD_MAX_SUPERVISORS_RANGE):
        limit = DEFAULT_STEWARD_MAX_SUPERVISORS_PER_REPO
    return {
        "child_permission_mode": mode,
        "check_interval_seconds": interval,
        "max_supervisors_per_repo": limit,
    }


def load_steward_policy(config_path=None, topology_path=None):
    """The one reader the role grant and the check schedule share. Returns
    `(policy, warnings)`; an unreadable or invalid file yields the shipped
    defaults plus one `steward-settings-unreadable: <reason>` warning."""
    try:
        config = load_and_validate(
            config_path or default_config_path(),
            topology_path or default_topology_path(),
        )
    except (DefaultsConfigError, OSError, json.JSONDecodeError) as exc:
        return query_steward_policy({}), [f"steward-settings-unreadable: {exc}"]
    return query_steward_policy(config), []


def query_steward_child_permission_mode(config):
    """SD-122 (9): default child-session permission mode for `peer-steward.py
    start`. Returns `bypass` whenever the `steward` block or its
    `child_permission_mode` key is absent, including for schema_version < 4.
    """
    return query_steward_policy(config)["child_permission_mode"]


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
        print("usage: dispatch-defaults.py <validate|affinity|owners|policy|allocation|opencode-policy|confirmation-mode|steward> [options]", file=sys.stderr)
        return 64

    op = argv[0]
    rest = argv[1:]
    config_path = _arg(rest, "--config", default_config_path())
    topology_path = _arg(rest, "--topology", default_topology_path())

    try:
        config, policy_findings = load_validated(config_path, topology_path)
    except (DefaultsConfigError, OSError, json.JSONDecodeError) as exc:
        print(f"dispatch-defaults: invalid config {config_path}: {exc}", file=sys.stderr)
        return 65

    if op == "validate":
        print(f"dispatch-defaults: {config_path} is valid")
        # Warnings never change the exit code: a valid file stays valid. They
        # exist because "valid" alone hid two weeks of a balanced-first decision
        # that never reached the user-owned file (2026-08-13 -> 2026-08-29).
        for warning in [*policy_findings, *allocation_warnings(config, config_path)]:
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
    if op == "policy-receipt":
        profile = _arg(rest, "--profile", "light")
        policy = query_profile_policy(config, profile)
        source = ("legacy-schema" if config.get("schema_version", 1) < 3 else
                  "derived-from-user-light" if profile == "balanced" and profile not in config["profiles"]
                  else "explicit")
        print(json.dumps({"config_path": config_path, "profile": profile,
                          "source": source, "policy": policy}, sort_keys=True))
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
        if "owner_order" in allocation:
            print("owner_order=" + ",".join(allocation["owner_order"]))
        if allocation.get("harness_weights"):
            weights = allocation["harness_weights"]
            print("harness_weights=" + ",".join(f"{key}:{weights[key]}" for key in sorted(weights)))
        return 0
    if op == "opencode-policy":
        print(query_opencode_policy(config))
        return 0
    if op == "confirmation-mode":
        print(query_confirmation_mode(config))
        return 0
    if op == "steward":
        policy = query_steward_policy(config)
        for key in ("child_permission_mode", "check_interval_seconds", "max_supervisors_per_repo"):
            print(f"{key}={policy[key]}")
        return 0

    print(f"dispatch-defaults: unknown operation {op!r}", file=sys.stderr)
    return 64


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
