#!/bin/sh
# PreToolUse(Agent|Task): give native Claude subagent spawns the config-declared
# default model tier instead of silently inheriting the interactive session
# model (core/ADAPTATION.md §3). Injection re-emits the FULL tool_input with a
# `model` field added. Explicit eligible pins remain untouched; inherited/fork
# selection and config-declared main-session-only models are denied because a
# native worker must never inherit an interactive-only main model. Tier
# resolution selects the complete user model config when valid and the complete
# shipped Claude config otherwise. A local fixture layout keeps the direct
# shipped-file fallback; no concrete model ID is hardcoded here.
# Runtime override: CLAUDE_NATIVE_SUBAGENT_MODEL=<eligible-alias> forces the
# injected model. `inherit` and config-declared main-session-only aliases are
# typed denials. Malformed non-actionable input remains silent; a valid Agent
# request fails closed when its model policy cannot be loaded.
''''exec python3 "$0" "$@" # '''
import glob
import json
import os
import re
import sys

FRONTMATTER_MODEL = re.compile(r"^model:[ \t]*(.*?)[ \t]*$")


def parse_conf(path):
    conf = {}
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            val = val.strip()
            if val[:1] in ('"', "'"):
                val = val[1:].split(val[0], 1)[0]
            else:
                val = val.split("#", 1)[0].strip()
            conf[key.strip()] = val
    return conf


def model_conf(hook_dir):
    """Use the shared whole-file resolver in a checkout or installed bundle."""
    current = os.path.realpath(hook_dir)
    for _ in range(5):
        utility_dir = os.path.join(current, "utilities")
        shipped = os.path.join(
            current, "adapters", "claude", "config", "models.conf"
        )
        if os.path.isfile(os.path.join(utility_dir, "model_config.py")) and os.path.isfile(shipped):
            if utility_dir not in sys.path:
                sys.path.insert(0, utility_dir)
            from model_config import resolve_config
            values, _receipt = resolve_config("claude", source_root=current)
            return values
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent

    for candidate in (
        os.path.join(hook_dir, "..", "config", "models.conf"),
        os.path.join(hook_dir, "..", "adapters", "claude", "config", "models.conf"),
    ):
        if os.path.isfile(candidate):
            return parse_conf(candidate)
    return None


def frontmatter_model(path):
    """Return the frontmatter model pin, or None when the file pins nothing."""
    with open(path, encoding="utf-8") as fh:
        lines = fh.read().splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    for line in lines[1:]:
        if line.strip() in ("---", "..."):
            break
        match = FRONTMATTER_MODEL.match(line)
        if match:
            value = match.group(1).strip().strip("\"'")
            # Since v2.1.196 a frontmatter `inherit` value is treated as unset.
            if value and value != "inherit":
                return value
            return None
    return None


def agent_definition(name):
    """First existing definition file for the agent name, or None (built-in)."""
    home = os.path.expanduser("~")
    candidates = []
    project_dir = os.environ.get("CLAUDE_PROJECT_DIR", "").strip()
    if project_dir:
        candidates.append(os.path.join(project_dir, ".claude", "agents", name + ".md"))
    candidates.append(os.path.join(home, ".claude", "agents", name + ".md"))
    candidates.extend(sorted(glob.glob(
        os.path.join(home, ".claude", "plugins", "cache", "*", "*", "*", "agents", name + ".md"))))
    candidates.extend(sorted(glob.glob(
        os.path.join(home, ".claude", "plugins", "marketplaces", "*", "plugins", "*", "agents", name + ".md"))))
    for candidate in candidates:
        if os.path.isfile(candidate):
            return candidate
    return None


def deny(reason):
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": reason,
    }}))


def restricted_model(model, restricted):
    tokens = set(re.split(r"[^a-z0-9]+", str(model).lower()))
    return any(alias.lower() in tokens for alias in restricted)


def apply_policy(tool_input):
    hook_dir = os.path.dirname(os.path.realpath(__file__))
    conf = model_conf(hook_dir)
    if conf is None:
        deny("native-subagent-model-policy-unavailable")
        return
    if "CFG_MAIN_SESSION_ONLY_MODELS" not in conf:
        deny("native-subagent-model-policy-unavailable")
        return
    restricted = conf.get("CFG_MAIN_SESSION_ONLY_MODELS", "").split()

    explicit = str(tool_input.get("model") or "").strip()
    if explicit:
        if explicit.lower() == "inherit":
            deny("native-subagent-model-inheritance-ineligible")
        elif restricted_model(explicit, restricted):
            deny("native-subagent-main-session-only-model")
        return
    subagent_type = str(tool_input.get("subagent_type") or "")
    if subagent_type == "fork":
        deny("native-subagent-model-inheritance-ineligible")
        return
    # Namespaced plugin types (`plugin:agent`) resolve to the last segment.
    name = subagent_type.rsplit(":", 1)[-1]
    if name:
        definition = agent_definition(name)
        if definition:
            pinned = frontmatter_model(definition)
            if pinned:
                if restricted_model(pinned, restricted):
                    deny("native-subagent-main-session-only-model")
                return
    override = os.environ.get("CLAUDE_NATIVE_SUBAGENT_MODEL", "").strip()
    if override.lower() == "inherit":
        deny("native-subagent-model-inheritance-ineligible")
        return
    if override:
        if restricted_model(override, restricted):
            deny("native-subagent-main-session-only-model")
            return
        target = override
    else:
        tier = conf.get("CFG_NATIVE_SUBAGENT", "").strip()
        target = conf.get("CFG_TIER_%s_MODEL" % tier.upper(), "").strip() if tier else ""
    if not target or restricted_model(target, restricted):
        deny("native-subagent-eligible-model-unavailable")
        return
    updated = dict(tool_input)
    updated["model"] = target
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "updatedInput": updated,
    }}))


def main():
    try:
        payload = json.loads(sys.stdin.read())
    except Exception:
        return
    if not isinstance(payload, dict):
        return
    if payload.get("tool_name") not in ("Agent", "Task"):
        return
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return
    try:
        apply_policy(tool_input)
    except Exception:
        deny("native-subagent-model-policy-unavailable")


if __name__ == "__main__":
    main()
    sys.exit(0)
