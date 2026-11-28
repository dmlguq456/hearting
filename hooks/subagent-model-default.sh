#!/bin/sh
# PreToolUse(Agent|Task): the native call's model policy below decides first. With
# `--redirect`, Hearting may then take a call the policy lets through as a unit
# route (utilities/native_subagent_redirect.py, only when that is certain; the
# call is refused with one line naming the route). When Hearting does not, or
# without `--redirect` (the registration of a session opened before it), the
# native call runs as below: give native Claude subagent spawns the config-declared
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
    # The user-scope root is where this Claude actually reads definitions:
    # $CLAUDE_CONFIG_DIR when set (a worker's masked home carries none, so a
    # definition under the real ~/.claude is not loaded there), else ~/.claude.
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
    root = config_dir or os.path.join(os.path.expanduser("~"), ".claude")
    candidates = []
    project_dir = os.environ.get("CLAUDE_PROJECT_DIR", "").strip()
    if project_dir:
        candidates.append(os.path.join(project_dir, ".claude", "agents", name + ".md"))
    candidates.append(os.path.join(root, "agents", name + ".md"))
    candidates.extend(sorted(glob.glob(
        os.path.join(root, "plugins", "cache", "*", "*", "*", "agents", name + ".md"))))
    candidates.extend(sorted(glob.glob(
        os.path.join(root, "plugins", "marketplaces", "*", "plugins", "*", "agents", name + ".md"))))
    for candidate in candidates:
        if os.path.isfile(candidate):
            return candidate
    return None


def deny(reason):
    return {"hookEventName": "PreToolUse", "permissionDecision": "deny",
            "permissionDecisionReason": reason}


def restricted_model(model, restricted):
    # Hand-mirrored from utilities/model_config.py `restricted_model` (this
    # hook is embedded Python inside a shell file and imports nothing): an
    # entry matches as a whole id, or as a token when it is a bare alias.
    lowered = str(model).lower()
    tokens = set(re.split(r"[^a-z0-9]+", lowered))
    for alias in restricted:
        alias = alias.lower()
        if alias and (alias == lowered or (re.fullmatch(r"[a-z0-9]+", alias) and alias in tokens)):
            return True
    return False


def apply_policy(tool_input):
    """The native call's decision: a deny, the input with the model added, or None (as it is)."""
    hook_dir = os.path.dirname(os.path.realpath(__file__))
    conf = model_conf(hook_dir)
    if conf is None:
        return deny("native-subagent-model-policy-unavailable")
    if "CFG_MAIN_SESSION_ONLY_MODELS" not in conf:
        return deny("native-subagent-model-policy-unavailable")
    restricted = conf.get("CFG_MAIN_SESSION_ONLY_MODELS", "").split()

    explicit = str(tool_input.get("model") or "").strip()
    if explicit:
        if explicit.lower() == "inherit":
            return deny("native-subagent-model-inheritance-ineligible")
        if restricted_model(explicit, restricted):
            return deny("native-subagent-main-session-only-model")
        return None
    subagent_type = str(tool_input.get("subagent_type") or "")
    if subagent_type == "fork":
        return deny("native-subagent-model-inheritance-ineligible")
    # Namespaced plugin types (`plugin:agent`) resolve to the last segment.
    name = subagent_type.rsplit(":", 1)[-1]
    if name:
        definition = agent_definition(name)
        if definition:
            pinned = frontmatter_model(definition)
            if pinned:
                if restricted_model(pinned, restricted):
                    return deny("native-subagent-main-session-only-model")
                return None
    override = os.environ.get("CLAUDE_NATIVE_SUBAGENT_MODEL", "").strip()
    if override.lower() == "inherit":
        return deny("native-subagent-model-inheritance-ineligible")
    if override:
        if restricted_model(override, restricted):
            return deny("native-subagent-main-session-only-model")
        target = override
    else:
        tier = conf.get("CFG_NATIVE_SUBAGENT", "").strip()
        # Tier ids are normalized the way model_profile does: `balanced-deep` names
        # the CFG_TIER_BALANCED_DEEP_* keys.
        tier_key = tier.upper().replace("-", "_")
        target = conf.get("CFG_TIER_%s_MODEL" % tier_key, "").strip() if tier else ""
    if not target or restricted_model(target, restricted):
        return deny("native-subagent-eligible-model-unavailable")
    updated = dict(tool_input)
    updated["model"] = target
    return {"hookEventName": "PreToolUse", "updatedInput": updated}


def started_as_unit_route(payload):
    """The refusal line when the shared redirect took the call as a unit route, else None."""
    root = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
    if not os.path.isfile(os.path.join(root, "utilities", "native_subagent_redirect.py")):
        return None
    try:
        sys.path.insert(0, os.path.join(root, "utilities"))
        import native_subagent_redirect
        return native_subagent_redirect.redirect("claude", payload)
    except BaseException:
        return None


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
        decision = apply_policy(tool_input)
    except Exception:
        decision = deny("native-subagent-model-policy-unavailable")
    # A call this policy refuses is refused as before: nothing starts for it.
    refused = bool(decision) and decision.get("permissionDecision") == "deny"
    if "--redirect" in sys.argv[1:] and not refused:
        reason = started_as_unit_route(payload)
        if reason is not None:
            decision = deny(reason)
    if decision:
        print(json.dumps({"hookSpecificOutput": decision}))


if __name__ == "__main__":
    main()
    sys.exit(0)
