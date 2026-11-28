#!/usr/bin/env python3
"""Generate OpenCode-native Agent projections from the models.conf catalog.

Team agents are retired: former team behavior lives in the portable unit catalog
(`roles/units/**`) and runs as dispatched depth-2 nodes, never as native agents.
The catalog agents are the depth names a native subagent call gives (`light`,
`balanced`, `deep`, `general-purpose`); no kernel helper ships (`kernel.agents`
in `harness-manifest.json` is empty).

This file holds the OpenCode spec text and body format; the shared renderer in
`utilities/native_agents.py` plans, generates and checks them.
"""

from __future__ import annotations

import re
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
# catalog. Portable profiles resolve through utilities/model_profile.py; a native
# agent file pins the model only, and the registered-launch wrapper attaches the
# variant (--variant) for route-bound work.
# Instructions are adapter-owned output, not a non-OpenCode Agent copy.
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


def render_catalog_agent(name: str, profile: str, cfg: Mapping[str, str]) -> tuple[str, str]:
    spec = CATALOG_AGENTS.get(name)
    if spec is None:
        raise native_agents.RenderError(f"catalog names an agent with no OpenCode spec: {name}")
    resolved = resolve_profile_values("opencode", cfg, profile)
    description = compact(spec["description"])
    return f"""---
description: "{description}"
mode: subagent
model: {resolved["model"]}
---

{spec["instructions"]}""", resolved["model"]


def compact(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip()).replace('"', "'")


def render(name: str, profile: Optional[str], cfg: Mapping[str, str]) -> tuple[str, Optional[str]]:
    if profile is None:
        raise native_agents.RenderError(f"no OpenCode kernel agent ships: {name}")
    return render_catalog_agent(name, profile, cfg)


FORMAT = native_agents.Format(
    adapter="opencode",
    kernel_names=tuple(harness_manifest.load()["kernel"]["agents"]),
    static_names=(),
    shipped_relpath=lambda name: f"{name}/{name}.md",
    dest_name=lambda name: f"{name}.md",
    render=render,
    collect_glob="*/*.md",
)


if __name__ == "__main__":
    raise SystemExit(native_agents.main(FORMAT, sys.argv[1:]))
