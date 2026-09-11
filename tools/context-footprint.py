#!/usr/bin/env python3
"""Report early-context footprint for the portable agent harness.

This is a deterministic audit helper. It counts bootstrap bytes, active Skill
metadata surfaces, duplicate discovery, representative hook outputs, baseline
regression, and the largest Skill bodies. ``--strict`` makes every budget or
baseline warning fail.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Iterable

BOOTSTRAP_BUDGET = 16_384
WORKER_BOOTSTRAP_BUDGET = 4_096
SKILL_METADATA_BUDGET = 7_000
ENTRY_ROUTER_BUDGET = 4_096
ENTRY_ROUTER_TOTAL_BUDGET = 53_248
MAX_BASELINE_GROWTH = 1.05
ZERO_INJECTION_SAMPLES = {
    "codex-userprompt-default",
    "codex-briefing-default",
    "codex-token-unknown",
    "codex-token-repeated",
}


def chars(path: Path) -> int:
    try:
        return len(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return 0


def utf8_bytes(path: Path) -> int:
    try:
        return len(path.read_bytes())
    except FileNotFoundError:
        return 0


def load_baseline(path: Path) -> dict[str, int]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schema") != 1 or not isinstance(data.get("surfaces"), dict):
        raise ValueError("baseline must use schema=1 and contain a surfaces object")
    values: dict[str, int] = {}
    for key, value in data["surfaces"].items():
        if not isinstance(key, str) or not isinstance(value, int) or value < 0:
            raise ValueError(f"invalid baseline surface: {key}={value!r}")
        values[key] = value
    return values


def skill_meta(
    base: Path, prefix: str = "", selected: set[str] | None = None,
) -> tuple[int, int, int, set[str]]:
    normalized_total = 0
    local_path_total = 0
    count = 0
    names: set[str] = set()
    for skill in sorted(base.glob("*/SKILL.md")):
        if selected is not None and skill.parent.name not in selected:
            continue
        text = skill.read_text(encoding="utf-8", errors="ignore")
        name_match = re.search(r"^name:\s*(.+)$", text, re.M)
        desc_match = re.search(r"^description:\s*(.+)$", text, re.M)
        if not name_match or not desc_match:
            continue
        name = name_match.group(1).strip().strip('"')
        desc = desc_match.group(1).strip().strip('"')
        names.add(name)
        count += 1
        # Baseline the controllable payload with a relative path so checkout
        # location does not look like source regression. Separately retain the
        # concrete local path cost for the absolute discovery-surface budget.
        common = len(prefix + name) + len(desc)
        normalized_total += common + len(str(skill.relative_to(base)))
        local_path_total += common + len(str(skill))
    return normalized_total, local_path_total, count, names


def skill_body_sizes(paths: Iterable[Path]) -> list[tuple[int, str]]:
    rows: list[tuple[int, str]] = []
    for base in paths:
        for skill in sorted(base.glob("*/SKILL.md")):
            rows.append((chars(skill), str(skill)))
    return sorted(rows, reverse=True)


def entry_routers(root: Path) -> list[str]:
    manifest = json.loads((root / "harness-manifest.json").read_text(encoding="utf-8"))
    entries = sorted(name for name, spec in manifest["capabilities"].items()
                     if spec["invocation"]["class"] == "entry-router")
    # Count-bound on purpose: an unnoticed entry-router gain or loss silently
    # reshapes this report. Update it in the same commit that adds or retires a
    # router — 7283cd48 retired `autopilot-note` and left this at 13.
    if len(entries) != 12:
        raise ValueError(f"expected exactly 12 entry routers, got {len(entries)}")
    return entries


def run_capture(
    args: list[str], cwd: Path, env: dict[str, str] | None = None,
    timeout: int = 10, stdin: str | None = None,
) -> tuple[int, str]:
    merged = os.environ.copy()
    if env:
        merged.update(env)
    try:
        proc = subprocess.run(
            args,
            cwd=cwd,
            env=merged,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            input=stdin,
            timeout=timeout,
            check=False,
        )
        return proc.returncode, proc.stdout
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return 69, str(exc)


def plugin_install(
    root: Path, codex_home: Path | None, timeout: int,
) -> tuple[bool | None, Path | None]:
    if codex_home is None:
        return None, None
    rc, out = run_capture(["codex", "plugin", "list", "--json"], root, {"CODEX_HOME": str(codex_home)}, timeout)
    if rc != 0:
        return None, None
    try:
        installed = json.loads(out).get("installed", [])
    except (AttributeError, json.JSONDecodeError):
        return None, None
    for item in installed:
        if not isinstance(item, dict) or item.get("name") != "hearting-codex":
            continue
        if not item.get("installed") or not item.get("enabled"):
            return False, None
        marketplace = item.get("marketplaceName")
        version = item.get("version")
        if isinstance(marketplace, str) and isinstance(version, str):
            skills = codex_home / "plugins" / "cache" / marketplace / "hearting-codex" / version / "skills"
            if skills.is_dir():
                return True, skills
        return True, None
    return False, None


def harness_native_skill_links(root: Path, codex_home: Path | None) -> set[str]:
    if codex_home is None:
        return set()
    skills_home = codex_home / "skills"
    projected = (root / "codex_setting" / "codex-skills").resolve()
    names: set[str] = set()
    if not skills_home.exists():
        return names
    for item in skills_home.iterdir():
        if not item.is_symlink():
            continue
        try:
            target = item.resolve()
        except OSError:
            continue
        try:
            target.relative_to(projected)
        except ValueError:
            continue
        if (target / "SKILL.md").exists():
            names.add(item.name)
    return names


def hook_samples(root: Path, timeout: int) -> list[tuple[str, int, int]]:
    env = {"AGENT_HOME": str(root)}
    samples: list[tuple[str, int, int]] = []
    commands = [
        ("codex-recall-neutral", [str(root / "adapters/codex/bin/preflight.sh"), "recall", "오늘 작업", str(root)], None),
        ("codex-recall-signal", [str(root / "adapters/codex/bin/preflight.sh"), "recall", "지난번 작업", str(root)], None),
        ("codex-briefing-default", [str(root / "adapters/codex/bin/preflight.sh"), "briefing", str(root)], None),
        ("codex-token-unknown", [str(root / "adapters/codex/bin/preflight.sh"), "token-budget", str(root), "footprint-unknown", "hook"], None),
        ("codex-token-repeated", [str(root / "adapters/codex/bin/preflight.sh"), "token-budget", str(root), "footprint-unknown", "hook"], None),
        (
            "codex-userprompt-default",
            [sys.executable, str(root / "adapters/codex/hooks/userprompt-lifecycle.py")],
            json.dumps({"cwd": str(root), "session_id": "footprint-userprompt"}),
        ),
    ]
    with tempfile.TemporaryDirectory(prefix="context-footprint-mem-") as tmp:
        sample_env = dict(env)
        sample_env["MEM_STORE"] = tmp
        sample_env["XDG_STATE_HOME"] = tmp
        sample_env["CODEX_DISTILL_ENABLE"] = "0"
        for label, cmd, stdin in commands:
            rc, out = run_capture(cmd, root, sample_env, timeout, stdin)
            samples.append((label, rc, len(out)))
    return samples


def main() -> int:
    parser = argparse.ArgumentParser(description="Report agent harness context footprint")
    parser.add_argument("--root", default=".", help="harness repository root")
    parser.add_argument("--codex-home", default=os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
    parser.add_argument("--skip-runtime", action="store_true", help="do not inspect CODEX_HOME/plugin state")
    parser.add_argument("--skip-hooks", action="store_true", help="do not run hook/preflight samples")
    parser.add_argument("--timeout", type=int, default=10)
    parser.add_argument("--strict", action="store_true", help="exit 1 when warnings are present")
    parser.add_argument(
        "--baseline",
        default="tools/context-footprint-baseline.json",
        help="checked surface baseline, relative to root by default",
    )
    args = parser.parse_args()

    root = Path(args.root).resolve()
    codex_home = None if args.skip_runtime else Path(args.codex_home).expanduser().resolve()
    warnings: list[str] = []
    surfaces: dict[str, int] = {}

    print("context_footprint_report=1")
    print(f"root={root}")

    bootstraps = [
        root / "adapters/codex/AGENTS.md",
        root / "adapters/claude/CLAUDE.md",
        root / "adapters/opencode/AGENTS.md",
    ]
    print("\n[bootstrap]")
    for path in bootstraps:
        label = path.parent.name
        size_bytes = utf8_bytes(path)
        size_chars = chars(path)
        surfaces[f"bootstrap:{label}"] = size_bytes
        print(f"surface={label} bytes={size_bytes} chars={size_chars} path={path.relative_to(root)}")
        if size_bytes > BOOTSTRAP_BUDGET:
            warnings.append(f"{label} bootstrap {size_bytes} > {BOOTSTRAP_BUDGET} bytes")

    print("\n[worker-bootstrap]")
    kernel = root / "roles" / "worker-bootstrap.md"
    kernel_bytes = utf8_bytes(kernel)
    surfaces["worker-bootstrap:kernel"] = kernel_bytes
    print(f"surface=kernel bytes={kernel_bytes} path={kernel.relative_to(root)}")
    for worker_type in ("owner", "stage", "review", "support", "frame"):
        fragment = root / "roles" / "worker-types" / f"{worker_type}.md"
        combined = kernel_bytes + 2 + utf8_bytes(fragment)
        surfaces[f"worker-bootstrap:{worker_type}"] = combined
        print(
            f"surface={worker_type} bytes={combined} "
            f"fragment={fragment.relative_to(root)}"
        )
        if combined > WORKER_BOOTSTRAP_BUDGET:
            warnings.append(
                f"{worker_type} worker bootstrap {combined} > {WORKER_BOOTSTRAP_BUDGET} bytes"
            )

    print("\n[native-bootstrap]")
    agents_dir = root / "adapters/claude/agents"
    agents_total = 0
    for path in sorted(agents_dir.glob("*.md")):
        size_bytes = utf8_bytes(path)
        agents_total += size_bytes
        print(f"agent={path.stem} bytes={size_bytes}")
    surfaces["native-bootstrap:agents-total"] = agents_total
    print(f"surface=native-bootstrap-agents bytes={agents_total} path={agents_dir.relative_to(root)}")
    modes_dir = root / "roles/units"
    modes_total = sum(utf8_bytes(path) for path in sorted(modes_dir.rglob("*.md")))
    if modes_dir.is_dir():
        for group in sorted(path for path in modes_dir.iterdir() if path.is_dir()):
            group_bytes = sum(utf8_bytes(path) for path in group.rglob("*.md"))
            print(f"unit-family={group.name} bytes={group_bytes}")
    surfaces["unit-catalog:total"] = modes_total
    print(f"surface=unit-catalog bytes={modes_total} path={modes_dir.relative_to(root)}")

    print("\n[skill-metadata]")
    codex_local, codex_local_path, codex_local_count, codex_local_names = skill_meta(root / "adapters/codex/skills")
    codex_plugin, codex_plugin_path, codex_plugin_count, codex_plugin_names = skill_meta(
        root / "adapters/codex/plugins/hearting-codex/skills", "hearting-codex:"
    )
    opencode, opencode_path, opencode_count, _ = skill_meta(root / "adapters/opencode/skills")
    claude, claude_path, claude_count, _ = skill_meta(root / "adapters/claude/skills")
    print(f"surface=codex-local items={codex_local_count} baseline_chars={codex_local} local_path_chars={codex_local_path}")
    print(f"surface=codex-plugin items={codex_plugin_count} baseline_chars={codex_plugin} local_path_chars={codex_plugin_path}")
    print(f"surface=opencode items={opencode_count} baseline_chars={opencode} local_path_chars={opencode_path}")
    print(f"surface=claude items={claude_count} baseline_chars={claude} local_path_chars={claude_path}")
    surfaces.update({
        "metadata:codex-local": codex_local,
        "metadata:codex-plugin": codex_plugin,
        "metadata:opencode": opencode,
        "metadata:claude": claude,
    })
    for label, size in (
        ("codex-local", codex_local),
        ("codex-plugin", codex_plugin),
        ("opencode", opencode),
        ("claude", claude),
    ):
        if size > SKILL_METADATA_BUDGET:
            warnings.append(f"{label} skill metadata {size} > {SKILL_METADATA_BUDGET}")

    print("\n[entry-router-bodies]")
    for label, base in (
        ("canonical", root / "skills"),
        ("claude", root / "adapters/claude/skills"),
        ("claude-plugin", root / "adapters/claude/plugin-marketplace/plugins/hearting-claude/skills"),
        ("codex", root / "adapters/codex/skills"),
        ("opencode", root / "adapters/opencode/skills"),
    ):
        sizes = [utf8_bytes(base / name / "SKILL.md") for name in entry_routers(root)]
        total, maximum = sum(sizes), max(sizes)
        surfaces[f"entry-router:{label}:total"] = total
        surfaces[f"entry-router:{label}:max"] = maximum
        print(f"surface={label} entries={len(sizes)} bytes={total} max={maximum}")
        if total > ENTRY_ROUTER_TOTAL_BUDGET or maximum > ENTRY_ROUTER_BUDGET:
            warnings.append(f"{label} entry router bytes total={total} max={maximum} exceed budget")

    plugin_state, installed_plugin_skills = plugin_install(root, codex_home, args.timeout) if codex_home else (None, None)
    native_links = harness_native_skill_links(root, codex_home)
    if codex_home:
        active = 0
        active_local_path = 0
        active_labels: list[str] = []
        if native_links:
            native_active, native_active_path, _, _ = skill_meta(
                codex_home / "skills", selected=native_links
            )
            active += native_active
            active_local_path += native_active_path
            active_labels.append("native")
        if plugin_state is True:
            active += codex_plugin
            if installed_plugin_skills is None:
                warnings.append("codex active plugin cache path unavailable for exact metadata measurement")
                active_local_path += codex_plugin_path
            else:
                _, installed_path_chars, _, _ = skill_meta(installed_plugin_skills, "hearting-codex:")
                active_local_path += installed_path_chars
            active_labels.append("plugin")
        if not active_labels:
            active_labels.append("none-or-unknown")
        overlap = sorted(native_links & codex_plugin_names) if plugin_state is True else []
        print(f"codex_home={codex_home}")
        print(
            f"codex_active_surfaces={'+'.join(active_labels)} "
            f"baseline_chars={active} runtime_path_chars={active_local_path} duplicate_names={len(overlap)}"
        )
        if active_local_path > SKILL_METADATA_BUDGET:
            warnings.append(f"codex active skill metadata {active_local_path} > {SKILL_METADATA_BUDGET}")
        if overlap:
            warnings.append(f"codex duplicate skill names active={len(overlap)}")
    else:
        print("codex_active_surfaces=skipped")

    print("\n[hook-samples]")
    if args.skip_hooks:
        print("skipped=1")
    else:
        for label, rc, size in hook_samples(root, args.timeout):
            print(f"sample={label} exit={rc} chars={size}")
            surfaces[f"hook:{label}"] = size
            if rc != 0:
                warnings.append(f"{label} hook sample exited {rc}")
            if label in ZERO_INJECTION_SAMPLES and size > 0:
                warnings.append(f"{label} ordinary/unknown/repeated hook sample emitted {size} chars")

    print("\n[baseline]")
    baseline_path = Path(args.baseline)
    if not baseline_path.is_absolute():
        baseline_path = root / baseline_path
    try:
        baseline = load_baseline(baseline_path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"status=missing-or-invalid path={baseline_path} reason={exc}")
        warnings.append(f"context footprint baseline unavailable: {baseline_path}")
    else:
        print(f"status=loaded path={baseline_path} surfaces={len(baseline)} max_growth={MAX_BASELINE_GROWTH:.2f}")
        for key, current in sorted(surfaces.items()):
            if key not in baseline:
                warnings.append(f"surface missing from context footprint baseline: {key}")
                continue
            previous = baseline[key]
            limit = int(previous * MAX_BASELINE_GROWTH)
            print(f"surface={key} baseline={previous} current={current} limit={limit}")
            if current > limit:
                warnings.append(f"{key} footprint regression {current} > {limit} (baseline {previous})")
        for key in sorted(set(baseline) - set(surfaces)):
            if not (args.skip_hooks and key.startswith("hook:")):
                warnings.append(f"baseline surface was not measured: {key}")

    print("\n[largest-skill-bodies]")
    for size, path in skill_body_sizes([
        root / "adapters/claude/skills",
        root / "adapters/codex/skills",
        root / "adapters/opencode/skills",
    ])[:10]:
        try:
            rel = Path(path).relative_to(root)
        except ValueError:
            rel = Path(path)
        print(f"chars={size} path={rel}")

    print("\n[warnings]")
    if warnings:
        for warning in warnings:
            print(f"warning={warning}")
    else:
        print("none")
    print("status=ok" if not warnings else f"status=warn warnings={len(warnings)}")
    return 1 if args.strict and warnings else 0


if __name__ == "__main__":
    raise SystemExit(main())
