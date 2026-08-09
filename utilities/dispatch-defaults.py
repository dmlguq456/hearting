#!/usr/bin/env python3
"""Strict standard-library loader/validator/query CLI for profiles/dispatch-defaults.yaml (SD-66).

Parses a deliberately narrow YAML subset (scalars, inline lists, two-level
mappings, comments) — no PyYAML/yq dependency. Validates against the
canonical topology node ids in capabilities/topologies.json and exposes
affinity/owner/allocation queries used by utilities/dispatch-route.sh and its
fixture tests. Schema v1 remains readable as the former two-harness,
OpenCode-relief-only compatibility contract.
"""
import json
import os
import sys

LEGACY_NORMAL_HARNESSES = {"claude", "codex"}
DISPATCHABLE_HARNESSES = {"claude", "codex", "opencode"}
KNOWN_HARNESSES = DISPATCHABLE_HARNESSES
AFFINITY_VALUES = {"claude", "codex", "opencode", "diverse"}
ALLOCATION_STRATEGIES = {"least-recent-attempts"}
TOP_LEVEL_KEYS = {
    "schema_version", "depth1_owner", "opencode", "allocation", "capabilities"
}


class DefaultsConfigError(Exception):
    pass


def _repo_root():
    # realpath, not abspath: the helper is projected into
    # adapters/<harness>/utilities/ as a symlink, and the shipped config and
    # topology registry live only at the real repo root.
    return os.path.dirname(os.path.dirname(os.path.realpath(__file__)))


def default_config_path():
    return os.environ.get(
        "DISPATCH_DEFAULTS_CONFIG",
        os.path.join(_repo_root(), "profiles", "dispatch-defaults.yaml"),
    )


def default_topology_path():
    return os.path.join(_repo_root(), "capabilities", "topologies.json")


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
    elif version not in {1, 2}:
        errors.append(f"unsupported schema_version: {version!r}")

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

    allocation = config.get("allocation")
    if version == 2:
        if not isinstance(allocation, dict):
            errors.append("allocation must be a mapping for schema v2")
        else:
            unknown_allocation = sorted(set(allocation) - {"strategy", "window"})
            for key in unknown_allocation:
                errors.append(f"unknown allocation key: {key!r}")
            if allocation.get("strategy") not in ALLOCATION_STRATEGIES:
                errors.append(
                    "allocation.strategy must be least-recent-attempts"
                )
            window = allocation.get("window")
            if not isinstance(window, int) or not 3 <= window <= 300:
                errors.append("allocation.window must be an integer from 3 to 300")
    elif allocation is not None:
        errors.append("allocation requires schema_version 2")

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


def load_and_validate(config_path, topology_path):
    with open(config_path, encoding="utf-8") as f:
        text = f.read()
    config = parse_yaml_subset(text)
    capmap = load_topology_capabilities(topology_path)
    errors = validate(config, capmap)
    if errors:
        raise DefaultsConfigError("; ".join(errors))
    return config


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
    return config.get("depth1_owner", [])


def query_allocation(config):
    allocation = config.get("allocation")
    if config.get("schema_version") == 2 and isinstance(allocation, dict):
        return {
            "strategy": allocation["strategy"],
            "window": allocation["window"],
            "harness_order": list(query_owners(config)),
        }
    return {
        "strategy": "config-order",
        "window": 0,
        "harness_order": list(query_owners(config)),
    }


def query_opencode_policy(config):
    opencode = config.get("opencode", {})
    if isinstance(opencode, dict) and opencode.get("relief_only") is True:
        return "relief-only"
    if isinstance(opencode, dict) and opencode.get("relief_only") is False:
        return "normal"
    return "unknown"


def _arg(args, flag, default=None):
    if flag in args:
        i = args.index(flag)
        if i + 1 >= len(args):
            raise DefaultsConfigError(f"{flag} requires a value")
        return args[i + 1]
    return default


def main(argv):
    if not argv:
        print("usage: dispatch-defaults.py <validate|affinity|owners|allocation|opencode-policy> [options]", file=sys.stderr)
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
        return 0
    if op == "affinity":
        capability = _arg(rest, "--capability", "")
        stage = _arg(rest, "--stage", "")
        print(query_affinity(config, capability, stage))
        return 0
    if op == "owners":
        print(",".join(query_owners(config)))
        return 0
    if op == "allocation":
        allocation = query_allocation(config)
        print(f"strategy={allocation['strategy']}")
        print(f"window={allocation['window']}")
        print("harness_order=" + ",".join(allocation["harness_order"]))
        return 0
    if op == "opencode-policy":
        print(query_opencode_policy(config))
        return 0

    print(f"dispatch-defaults: unknown operation {op!r}", file=sys.stderr)
    return 64


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
