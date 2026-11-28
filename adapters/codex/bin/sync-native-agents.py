#!/usr/bin/env python3
"""Generate Codex-native custom agent projections from the models.conf catalog.

Team agents are retired: former team behavior lives in the portable unit catalog
(`roles/units/**`) and runs as dispatched depth-2 nodes, never as native agents.
The catalog agents are the depth names a native subagent call gives (`light`,
`balanced`, `deep`, `general-purpose`); no kernel helper ships (`kernel.agents`
in `harness-manifest.json` is empty).

This file holds the Codex spec text and body format; the shared renderer in
`utilities/native_agents.py` plans, generates and checks them.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Mapping, Optional


ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "utilities"))

import harness_manifest
import native_agents
from model_profile import resolve_profile_values


# Native subagent type catalog (CFG_NATIVE_AGENT_CATALOG in this adapter's
# models.conf, name:portable-profile) — cross-harness parity with the Claude
# catalog (adapters/claude/bin/sync-native-agents.py). Portable profiles resolve
# through utilities/model_profile.py; the sandbox stays workspace-write (these
# are general delegated workers).
# Instructions are adapter-owned output, not a Claude Agent copy.
CATALOG_SANDBOX = "workspace-write"

# A person's session's call of a catalog agent starts a unit route (core/OPERATIONS.md §5.10c); the
# same definition serves a registered worker, whose call runs natively, so the line names the session.
_UNIT_ROUTE = (" In a person's session the call starts a read-only Hearting unit route "
               "(general/explore, {depth}) when that is certain, and otherwise runs natively.")

CATALOG_AGENTS = {
    "balanced": {
        "description": ("Execute an approved decision across multiple steps on the balanced budget."
                        + _UNIT_ROUTE.format(depth="balanced")),
        "instructions": """Carry out the approved execution scope and cite its decision handoff.
Stop and return new important choices or uncertainty to the judgment owner.
Do not make architecture or policy decisions, widen scope, or spawn further agents.
Report completed steps, verification and remaining gaps.
""",
    },
    "general-purpose": {
        "description": (
            "General-purpose delegated agent for research, code search, and "
            "multi-step tasks on the balanced-deep budget." + _UNIT_ROUTE.format(depth="balanced-deep")
        ),
        "instructions": """You are a delegated general-purpose agent.
Complete the assigned task fully — don't gold-plate, but don't leave it half-done.
Search broadly when the target is unknown; read directly when the path is known.
Never create files unless necessary; prefer editing existing files.
Do not re-delegate the whole assignment to another agent.
Finish with a concise report of what was done and the key findings.
""",
    },
    "light": {
        "description": (
            "Breadth fan-out agent on the portable light budget — parallel "
            "sweeps, audits, comparisons, routine searches." + _UNIT_ROUTE.format(depth="light")
        ),
        "instructions": """You are one leg of a breadth fan-out on a light execution budget.
Stay inside the assigned slice; do not widen scope to neighboring questions.
Return compact, deduplicated findings — the caller merges many legs.
Do not spawn further agents; escalate by reporting, not delegating.
""",
    },
    "deep": {
        "description": (
            "Deep investigation agent on the portable deep budget — root cause, "
            "architecture, subtle correctness." + _UNIT_ROUTE.format(depth="deep")
        ),
        "instructions": """You are the deep leg of an investigation on the deep execution budget.
Ground every claim in files you actually read; quote paths and lines.
Distinguish verified facts from inference and state residual uncertainty.
Prefer one airtight answer over broad partial coverage.
""",
    },
}


# A pin is written only where the runtime home's provider serves it. The shipped
# models are account models of Codex's own provider; on another provider Codex
# cannot resolve the pinned model's metadata, refuses the spawn ("agent type is
# currently not available") and the parent sees only that tool error (PG-52).
# There the agent keeps its effort and sandbox and follows the session model.
UNPINNED_KEY = "_CODEX_AGENT_MODEL_UNPINNED"
ACCOUNT_PROVIDERS = frozenset({"openai"})


def selectable_providers(runtime_home: Path) -> frozenset[str]:
    """Every `model_provider` a session of this home can select.

    One set of agent files serves every session of the home, so the providers of
    all its configurations count: the top-level `config.toml`, each profile file
    `<name>.config.toml` that `codex --profile <name>` selects (Codex 0.134+;
    a file whose name Codex refuses as a profile is never selected), and the
    older `[profiles.*]` tables. An unset or unreadable provider is "openai".
    A provider given only on the command line (`-c model_provider=...`) is not
    in these files and is not seen.
    """
    import tomllib
    home = Path(runtime_home)
    providers = set()
    for path in (home / "config.toml", *native_agents.codex_profile_configs(home)):
        try:
            config = tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, ValueError):
            config = {}
        tables = [config]
        profiles = config.get("profiles")
        if isinstance(profiles, dict):
            tables += [table for table in profiles.values() if isinstance(table, dict)]
        for table in tables:
            provider = table.get("model_provider")
            providers.add(provider if isinstance(provider, str) and provider else "openai")
    return frozenset(providers or {"openai"})


# The models and efforts every session of the home is confirmed to spawn with, as JSON
# `{slug: [efforts]}`; absent when that cannot be confirmed (PG-13).
LISTED_KEY = "_CODEX_HOME_LISTED_MODELS"


def listed_models(runtime_home: Path) -> Optional[dict]:
    """`{slug: [efforts]}` every session of the home can spawn with, or None when unconfirmed."""
    return native_agents.codex_listed_models(runtime_home)


def runtime_config(cfg: Mapping[str, str], runtime_home: Path) -> Mapping[str, str]:
    import json
    listed = listed_models(runtime_home)
    if listed is not None:
        cfg = {**cfg, LISTED_KEY: json.dumps(listed, sort_keys=True)}
    if selectable_providers(runtime_home) <= ACCOUNT_PROVIDERS:
        return cfg
    return {**cfg, UNPINNED_KEY: "1"}


def model_line(model: str, cfg: Mapping[str, str]) -> str:
    return "" if cfg.get(UNPINNED_KEY) else f'model = "{toml_string(model)}"\n'


def toml_string(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"')


def toml_multiline(text: str) -> str:
    return text.replace('"""', '\\"\\"\\"')


def render_catalog_agent(name: str, profile: str, cfg: Mapping[str, str]) -> tuple[str, str]:
    spec = CATALOG_AGENTS.get(name)
    if spec is None:
        raise native_agents.RenderError(f"catalog names an agent with no Codex spec: {name}")
    resolved = resolve_profile_values("codex", cfg, profile)
    pinned = None if cfg.get(UNPINNED_KEY) else resolved["model"]
    return f'''name = "{toml_string(name)}"
description = "{toml_string(spec["description"])}"
{model_line(resolved["model"], cfg)}model_reasoning_effort = "{toml_string(resolved["budget"])}"
sandbox_mode = "{toml_string(CATALOG_SANDBOX)}"
developer_instructions = """
{toml_multiline(spec["instructions"])}"""
''', pinned


def render(name: str, profile: Optional[str], cfg: Mapping[str, str]) -> tuple[str, Optional[str]]:
    if profile is None:
        raise native_agents.RenderError(f"no Codex kernel agent ships: {name}")
    return render_catalog_agent(name, profile, cfg)


def default_subagent(cfg: Mapping[str, str]) -> Optional[dict]:
    """`[agents] default_subagent_model`/`_reasoning_effort` for this home (PG-13).

    Codex gives a spawn with no model and an agent with no `model` the parent's
    model; this default takes its place (an explicit spawn model and an agent
    file's own `model` still win). Codex checks the default against the spawn
    list of the session and refuses every such spawn when it is not there, so the
    default is written only where every session of this home names a
    `model_catalog_json` whose list accepts the model and its effort under any
    auth (`native_agents.codex_listed_models`). A provider name or a
    `models_cache.json` is no such evidence; elsewhere the parent's model stays.
    """
    import json
    if cfg.get(UNPINNED_KEY) or not cfg.get(LISTED_KEY):
        return None
    resolved = native_agents.native_subagent_default("codex", cfg)
    if resolved is None:
        return None
    try:
        listed = json.loads(cfg[LISTED_KEY])
    except ValueError:
        return None
    efforts = listed.get(resolved["model"]) if isinstance(listed, dict) else None
    if not isinstance(efforts, list) or resolved["budget"] not in efforts:
        return None
    return {"default_subagent_model": resolved["model"],
            "default_subagent_reasoning_effort": resolved["budget"]}


FORMAT = native_agents.Format(
    adapter="codex",
    kernel_names=tuple(harness_manifest.load()["kernel"]["agents"]),
    static_names=(),
    shipped_relpath=lambda name: f"{name}.toml",
    dest_name=lambda name: f"{name}.toml",
    render=render,
    collect_glob="*.toml",
    runtime_config=runtime_config,
    default_subagent=default_subagent,
)


if __name__ == "__main__":
    raise SystemExit(native_agents.main(FORMAT, sys.argv[1:]))
