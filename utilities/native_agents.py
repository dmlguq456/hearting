#!/usr/bin/env python3
"""One renderer for the Claude, Codex and OpenCode native agent definitions.

Each `adapters/<runtime>/bin/sync-native-agents.py` declares a `Format` (its
spec text, output paths and body renderer) and delegates here. Two callers use
the same planning code:

* `generate` renders the shipped `models.conf` into `adapters/<runtime>/agents/`
  (`tools/generate.py`, `--check`).
* `runtime_plan` renders the `models.conf` that a runtime home actually selects
  (the user's whole-file copy, else the shipped file) and writes nothing. The
  installer runs it as `sync-native-agents.py --runtime-home HOME --json`.

Every agent ends in exactly one state; there is no "active/inactive" flag:

    shipped   rendered bytes equal the shipped file (the installer links it)
    rendered  rendered bytes differ from the shipped file (installer writes it)
    withheld  its model is main-session-only in the selected config: no file
    skipped   the selected config names something this renderer cannot render

An empty result (every agent withheld) is a normal result, not a fallback to
the shipped definitions.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

import model_config  # noqa: E402
import model_profile  # noqa: E402

SCHEMA = "hearting.native-agents/v1"
LABELS = {"claude": "Claude", "codex": "Codex", "opencode": "OpenCode"}


class RenderError(ValueError):
    """One agent cannot be rendered from the given config."""


@dataclass(frozen=True)
class Format:
    adapter: str                              # "claude" | "codex" | "opencode"
    kernel_names: tuple[str, ...]             # kernel helpers this adapter generates
    static_names: tuple[str, ...]             # hand-written shipped files, never rendered
    shipped_relpath: Callable[[str], str]     # path below adapters/<adapter>/agents
    dest_name: Callable[[str], str]           # file name below <runtime home>/agents
    # (name, profile or None for a kernel helper, config) -> (body, model or None)
    render: Callable[[str, Optional[str], Mapping[str, str]], tuple[str, Optional[str]]]
    collect_glob: Optional[str] = None        # stale-file sweep on write; None keeps strays

    @property
    def label(self) -> str:
        return LABELS[self.adapter]

    @property
    def out_dir(self) -> Path:
        return ROOT / "adapters" / self.adapter / "agents"

    @property
    def shipped_conf(self) -> Path:
        return model_config.shipped_path(self.adapter, source_root=ROOT)


@dataclass(frozen=True)
class Agent:
    name: str
    shipped_relpath: str
    dest_name: str
    status: str                               # render | withheld | skipped
    reason: str = ""
    body: str = ""
    model: Optional[str] = None


def catalog_tokens(cfg: Mapping[str, str]) -> tuple[list[tuple[str, str]], list[str]]:
    """Return (valid (name, profile) entries, problems) for CFG_NATIVE_AGENT_CATALOG."""
    entries: list[tuple[str, str]] = []
    problems: list[str] = []
    for token in cfg.get("CFG_NATIVE_AGENT_CATALOG", "").split():
        if token.count(":") != 1:
            problems.append(f"malformed CFG_NATIVE_AGENT_CATALOG entry: {token!r}")
            continue
        name, profile = token.split(":", 1)
        if profile == "top":
            # A native subagent can never run the main-session-only model
            # (the admission hook refuses it at every spawn), so a catalog
            # entry pinning it would only generate a definition that always
            # fails (top review m4).
            problems.append(f"CFG_NATIVE_AGENT_CATALOG entry {token!r}: top is not a native agent profile")
            continue
        entries.append((name, profile))
    return entries, problems


def parse_catalog(cfg: Mapping[str, str]) -> list[tuple[str, str]]:
    """Strict form for the shipped catalog: any problem, or an empty catalog, is fatal."""
    entries, problems = catalog_tokens(cfg)
    if problems:
        raise SystemExit(problems[0])
    if not entries:
        raise SystemExit("CFG_NATIVE_AGENT_CATALOG is empty or missing")
    return entries


def plan_agents(fmt: Format, cfg: Mapping[str, str]) -> list[Agent]:
    """Render kernel helpers then the catalog from `cfg`; one Agent per name."""
    restricted = model_config.main_session_only_models(cfg)
    entries, problems = catalog_tokens(cfg)
    requests: list[tuple[str, Optional[str]]] = [(name, None) for name in fmt.kernel_names]
    requests.extend(entries)
    agents = [
        Agent(token, "", "", "skipped", f"unrenderable: {token}")
        for token in problems
    ]
    seen: set[str] = set()
    for name, profile in requests:
        if name in seen:
            continue
        seen.add(name)
        relpath = fmt.shipped_relpath(name)
        dest = fmt.dest_name(name)
        try:
            body, model = fmt.render(name, profile, cfg)
        except RenderError as exc:
            agents.append(Agent(name, relpath, dest, "skipped", str(exc)))
            continue
        except (KeyError, ValueError) as exc:  # includes ModelProfileError
            agents.append(Agent(name, relpath, dest, "skipped", f"unrenderable: {exc}"))
            continue
        if model is not None and model_config.restricted_model(model, restricted):
            agents.append(Agent(name, relpath, dest, "withheld",
                                f"{model} is main-session-only in the selected models.conf",
                                model=model))
            continue
        agents.append(Agent(name, relpath, dest, "render", body=body, model=model))
    return agents


def _file_sha(path: Path) -> Optional[str]:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def runtime_plan(fmt: Format, runtime_home: Path) -> dict:
    """Plan the agents `runtime_home` selects. Reads config and shipped files only."""
    cfg, receipt = model_config.resolve_config(fmt.adapter, runtime=runtime_home, source_root=ROOT)
    if receipt.reason == "user-valid-derived-balanced":
        selected, _ = catalog_tokens(cfg)
        shipped, _ = catalog_tokens(model_profile.load_config(fmt.shipped_conf))
        if not any(name == "balanced" for name, _ in selected):
            balanced = next((f"{name}:{profile}" for name, profile in shipped
                             if name == "balanced"), None)
            if balanced is not None:
                cfg = dict(cfg)
                cfg["CFG_NATIVE_AGENT_CATALOG"] = " ".join(
                    [*cfg.get("CFG_NATIVE_AGENT_CATALOG", "").split(), balanced]
                )
    rows = []
    for agent in plan_agents(fmt, cfg):
        row = {
            "name": agent.name,
            "dest_name": agent.dest_name,
            "shipped_relpath": agent.shipped_relpath,
            "status": agent.status,
            "model": agent.model,
            "reason": agent.reason,
        }
        if agent.status == "render":
            shipped = fmt.out_dir / agent.shipped_relpath
            try:
                same = shipped.is_file() and shipped.read_text(encoding="utf-8") == agent.body
            except (OSError, UnicodeDecodeError):
                same = False
            row["status"] = "shipped" if same else "rendered"
            if not same:
                row["content"] = agent.body
        rows.append(row)
    return {
        "schema": SCHEMA,
        "adapter": fmt.adapter,
        "config": {"source": receipt.source, "reason": receipt.reason, "path": receipt.selected_path},
        "static_names": list(fmt.static_names),
        "agents": rows,
    }


def generate(fmt: Format, check: bool) -> int:
    """Render the shipped config into adapters/<adapter>/agents/ (write or --check)."""
    cfg = model_profile.load_config(fmt.shipped_conf)
    parse_catalog(cfg)
    agents = plan_agents(fmt, cfg)
    broken = [a for a in agents if a.status != "render"]
    if broken:
        for agent in broken:
            print(f"{fmt.label} native agent {agent.name}: {agent.status} ({agent.reason})",
                  file=sys.stderr)
        return 1

    out = fmt.out_dir
    expected = {out / a.shipped_relpath: a.body for a in agents}
    stale: list[str] = []
    for path, body in expected.items():
        if check:
            if not path.exists() or path.read_text(encoding="utf-8") != body:
                stale.append(str(path.relative_to(ROOT)))
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body, encoding="utf-8")

    if fmt.collect_glob:
        extras = [p for p in sorted(out.glob(fmt.collect_glob)) if p not in expected] if out.exists() else []
        if check:
            stale.extend(str(p.relative_to(ROOT)) for p in extras)
        else:
            for path in extras:
                path.unlink()
                if path.parent != out:
                    try:
                        path.parent.rmdir()
                    except OSError:
                        pass

    if stale:
        print(f"{fmt.label} native agent projections are stale:", file=sys.stderr)
        for item in stale:
            print(f"  {item}", file=sys.stderr)
        return 1
    if not check:
        print(f"generated {len(expected)} {fmt.label} native agent projections")
    return 0


def main(fmt: Format, argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="verify generated projections")
    parser.add_argument("--runtime-home", help="plan the agents this runtime home selects (needs --json)")
    parser.add_argument("--json", action="store_true", help="print the runtime plan as JSON")
    args = parser.parse_args(argv)
    if bool(args.runtime_home) != args.json:
        parser.error("--runtime-home and --json go together")
    if args.runtime_home:
        try:
            plan = runtime_plan(fmt, Path(args.runtime_home))
        except (model_config.ModelConfigError, OSError) as exc:
            print(f"{fmt.label} native agent plan failed: {exc}", file=sys.stderr)
            return 1
        print(json.dumps(plan, ensure_ascii=False, sort_keys=True))
        return 0
    return generate(fmt, args.check)
