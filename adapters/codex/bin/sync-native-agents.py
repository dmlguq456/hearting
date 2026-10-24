#!/usr/bin/env python3
"""Generate Codex-native custom agent projections for kernel agents.

Team agents are retired: former team behavior lives in the portable unit catalog
(`roles/units/**`) and runs as dispatched depth-2 nodes, never as native agents.
Only kernel helpers (`kernel.agents` in `harness-manifest.json`) project here.

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
from model_profile import load_config, resolve_profile_values


MODELS_CONF = ROOT / "adapters" / "codex" / "config" / "models.conf"
_CFG = load_config(MODELS_CONF)


KERNEL_AGENTS = {
    "memory-scout": {
        "description": "Read-only memory scout for agent-initiated deep memory reconnaissance.",
        "instructions": """You are the Codex-native memory-scout custom agent.
This is adapter-owned output generated from core/MEMORY.md §7.4, not a Claude Agent copy.

Contract:
1. Read-only only. Do not edit files or write memory.
2. Never run memory mutation commands such as mem add, mem note, mem consume, mem restore, mem delete, mem reinforce, mem merge, mem prune, mem graduate, or mem reattribute.
3. Use <agent-home>/tools/memory/recall.sh first in the current cwd with narrow synonym and Korean/English variants.
4. Read one selected hit with python3 <agent-home>/tools/memory/mem.py show <id>, or a small ranked set with <agent-home>/tools/memory/recall.sh "<query>" --full --limit 3. These reads do not consume pending handoffs.
5. If misses matter, expand to --all, then --sessions. Never bypass the CLI with direct SQLite or dump.jsonl reads.
6. Cross-check one live file/code fact when the memory result implies an actionable convention.

Output at most 15 lines:
- verdict: found / not-found / ambiguous
- hits: up to 3 short quotes or paraphrases with record id / session pointer
- apply: one line telling the main agent what to do now
- check: one live-code or file cross-check line, or not checked with reason
""",
    }
}

# Backward-compatible alias for callers that patched/consumed the old constant name.
EXTRA_AGENTS = KERNEL_AGENTS


# Native subagent type catalog (CFG_NATIVE_AGENT_CATALOG in this adapter's
# models.conf, name:portable-profile) — cross-harness parity with the Claude
# catalog (adapters/claude/bin/sync-native-agents.py). Portable profiles resolve
# through utilities/model_profile.py; the sandbox stays workspace-write (these
# are general delegated workers, not the read-only memory-scout shape).
# Instructions are adapter-owned output, not a Claude Agent copy.
CATALOG_SANDBOX = "workspace-write"

CATALOG_AGENTS = {
    "balanced": {
        "description": "Execute an approved decision across multiple steps on the balanced budget.",
        "instructions": """Carry out the approved execution scope and cite its decision handoff.
Stop and return new important choices or uncertainty to the judgment owner.
Do not make architecture or policy decisions, widen scope, or spawn further agents.
Report completed steps, verification and remaining gaps.
""",
    },
    "general-purpose": {
        "description": (
            "General-purpose delegated agent for research, code search, and "
            "multi-step tasks on the balanced-deep budget."
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
            "sweeps, audits, comparisons, routine searches."
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
            "architecture, subtle correctness."
        ),
        "instructions": """You are the deep leg of an investigation on the deep execution budget.
Ground every claim in files you actually read; quote paths and lines.
Distinguish verified facts from inference and state residual uncertainty.
Prefer one airtight answer over broad partial coverage.
""",
    },
}


def toml_string(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"')


def toml_multiline(text: str) -> str:
    return text.replace('"""', '\\"\\"\\"')


def codex_config(profile: str, cfg: Optional[Mapping[str, str]] = None) -> tuple[str, str, str]:
    """Model, effort and sandbox of a kernel helper; `cfg` defaults to the shipped config."""
    cfg = _CFG if cfg is None else cfg
    key = "CFG_PROFILE_" + profile.upper().replace("-", "_")
    tier, effort, sandbox = (cfg.get(key) or cfg["CFG_PROFILE_DEFAULT"]).split(":")
    return cfg[f"CFG_TIER_{tier.upper()}_MODEL"], effort, sandbox


def render_kernel_agent(name: str, spec: dict[str, str], cfg: Optional[Mapping[str, str]] = None) -> tuple[str, str]:
    model, reasoning, sandbox = codex_config(name, cfg)
    return f'''name = "{toml_string(name)}"
description = "{toml_string(spec["description"])}"
model = "{toml_string(model)}"
model_reasoning_effort = "{toml_string(reasoning)}"
sandbox_mode = "{toml_string(sandbox)}"
developer_instructions = """
{toml_multiline(spec["instructions"])}"""
''', model


def render_catalog_agent(name: str, profile: str, cfg: Mapping[str, str]) -> tuple[str, str]:
    spec = CATALOG_AGENTS.get(name)
    if spec is None:
        raise native_agents.RenderError(f"catalog names an agent with no Codex spec: {name}")
    resolved = resolve_profile_values("codex", cfg, profile)
    return f'''name = "{toml_string(name)}"
description = "{toml_string(spec["description"])}"
model = "{toml_string(resolved["model"])}"
model_reasoning_effort = "{toml_string(resolved["budget"])}"
sandbox_mode = "{toml_string(CATALOG_SANDBOX)}"
developer_instructions = """
{toml_multiline(spec["instructions"])}"""
''', resolved["model"]


def render(name: str, profile: Optional[str], cfg: Mapping[str, str]) -> tuple[str, Optional[str]]:
    if profile is None:
        spec = KERNEL_AGENTS.get(name)
        if spec is None:
            raise native_agents.RenderError(f"unknown kernel agent (no Codex projection defined): {name}")
        return render_kernel_agent(name, spec, cfg)
    return render_catalog_agent(name, profile, cfg)


FORMAT = native_agents.Format(
    adapter="codex",
    kernel_names=tuple(harness_manifest.load()["kernel"]["agents"]),
    static_names=(),
    shipped_relpath=lambda name: f"{name}.toml",
    dest_name=lambda name: f"{name}.toml",
    render=render,
    collect_glob="*.toml",
)


if __name__ == "__main__":
    raise SystemExit(native_agents.main(FORMAT, sys.argv[1:]))
