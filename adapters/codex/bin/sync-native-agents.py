#!/usr/bin/env python3
"""Generate Codex-native custom agent projections for kernel agents.

Team agents are retired: former team behavior lives in the portable unit catalog
(`roles/units/**`) and runs as dispatched depth-2 nodes, never as native agents.
Only kernel helpers (`kernel.agents` in `harness-manifest.json`) project here.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
OUT = ROOT / "adapters" / "codex" / "agents"
sys.path.insert(0, str(ROOT / "tools"))

import harness_manifest


MODELS_CONF = ROOT / "adapters" / "codex" / "config" / "models.conf"


def load_models_conf() -> dict[str, str]:
    """Parse the flat KEY=value config that is the sole source of concrete models."""
    cfg: dict[str, str] = {}
    for raw in MODELS_CONF.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = key.strip()
        val = val.strip()
        if val[:1] in ('"', "'"):
            end = val.find(val[0], 1)
            val = val[1:end] if end != -1 else val[1:]
        else:
            hidx = val.find("#")
            if hidx != -1:
                val = val[:hidx].strip()
        cfg[key] = val
    return cfg


_CFG = load_models_conf()


KERNEL_AGENTS = {
    "memory-scout": {
        "description": "Read-only memory scout for agent-initiated deep memory reconnaissance.",
        "instructions": """You are the Codex-native memory-scout custom agent.
This is adapter-owned output generated from core/MEMORY.md §7.4, not a Claude Agent copy.

Contract:
1. Read-only only. Do not edit files or write memory.
2. Never run memory mutation commands such as mem add, mem note, mem consume, mem restore, mem delete, mem reinforce, mem merge, mem prune, mem graduate, or mem reattribute.
3. Use <agent-home>/tools/memory/recall.sh first in the current cwd with --no-touch and narrow synonym and Korean/English variants.
4. Read one selected hit with python3 <agent-home>/tools/memory/mem.py show <id> --no-touch, or a small ranked set with <agent-home>/tools/memory/recall.sh "<query>" --full --limit 3 --no-touch. These reads do not update access dates or lookup events and do not consume pending handoffs.
5. If misses matter, expand to --all --no-touch, then --sessions. Never bypass the CLI with direct SQLite or dump.jsonl reads.
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


def parse_native_catalog() -> list[tuple[str, str]]:
    raw = _CFG.get("CFG_NATIVE_AGENT_CATALOG", "")
    entries = []
    for token in raw.split():
        if token.count(":") != 1:
            print(f"malformed CFG_NATIVE_AGENT_CATALOG entry: {token!r}", file=sys.stderr)
            raise SystemExit(1)
        name, profile = token.split(":", 1)
        if profile == "top":
            # A native subagent can never run the main-session-only model
            # (the admission hook refuses it at every spawn), so a catalog
            # entry pinning it would only generate a definition that always
            # fails (top review m4).
            raise SystemExit(f"CFG_NATIVE_AGENT_CATALOG entry {token!r}: top is not a native agent profile")
        entries.append((name, profile))
    return entries


def toml_string(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"')


def toml_multiline(text: str) -> str:
    return text.replace('"""', '\\"\\"\\"')


def codex_config(profile: str) -> tuple[str, str, str]:
    key = "CFG_PROFILE_" + profile.upper().replace("-", "_")
    tier, effort, sandbox = (_CFG.get(key) or _CFG["CFG_PROFILE_DEFAULT"]).split(":")
    return _CFG[f"CFG_TIER_{tier.upper()}_MODEL"], effort, sandbox


def render_kernel_agent(name: str, spec: dict[str, str]) -> str:
    model, reasoning, sandbox = codex_config(name)
    return f'''name = "{toml_string(name)}"
description = "{toml_string(spec["description"])}"
model = "{toml_string(model)}"
model_reasoning_effort = "{toml_string(reasoning)}"
sandbox_mode = "{toml_string(sandbox)}"
developer_instructions = """
{toml_multiline(spec["instructions"])}"""
'''


def render_catalog_agent(name: str, profile: str) -> str:
    spec = CATALOG_AGENTS.get(name)
    if spec is None:
        print(f"catalog names an agent with no Codex spec: {name}", file=sys.stderr)
        raise SystemExit(1)
    sys.path.insert(0, str(ROOT / "utilities"))
    from model_profile import resolve_profile
    resolved = resolve_profile("codex", MODELS_CONF, profile)
    return f'''name = "{toml_string(name)}"
description = "{toml_string(spec["description"])}"
model = "{toml_string(resolved["model"])}"
model_reasoning_effort = "{toml_string(resolved["budget"])}"
sandbox_mode = "{toml_string(CATALOG_SANDBOX)}"
developer_instructions = """
{toml_multiline(spec["instructions"])}"""
'''


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="verify generated projections")
    args = parser.parse_args()

    manifest = harness_manifest.load()
    expected: dict[Path, str] = {}
    for name in manifest["kernel"]["agents"]:
        spec = KERNEL_AGENTS.get(name)
        if spec is None:
            print(f"unknown kernel agent (no Codex projection defined): {name}", file=sys.stderr)
            return 1
        expected[OUT / f"{name}.toml"] = render_kernel_agent(name, spec)
    for name, profile in parse_native_catalog():
        expected[OUT / f"{name}.toml"] = render_catalog_agent(name, profile)

    stale: list[str] = []
    for path, body in expected.items():
        if args.check:
            if not path.exists() or path.read_text(encoding="utf-8") != body:
                stale.append(str(path.relative_to(ROOT)))
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body, encoding="utf-8")

    existing = sorted(OUT.glob("*.toml")) if OUT.exists() else []
    extras = [path for path in existing if path not in expected]
    if args.check:
        stale.extend(str(path.relative_to(ROOT)) for path in extras)
    else:
        for path in extras:
            path.unlink()

    if stale:
        print("Codex native agent projections are stale:", file=sys.stderr)
        for item in stale:
            print(f"  {item}", file=sys.stderr)
        return 1

    if not args.check:
        print(f"generated {len(expected)} Codex native agent projections")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
