"""Create the user-owned routing policy once from activated runtimes."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys

import paths


RUNTIMES = ("claude", "codex", "opencode")


def config_path() -> Path:
    return paths.hearting_config_dir() / "dispatch-defaults.yaml"


def available_runtimes(targets) -> list[str]:
    ordered = [name for name in RUNTIMES if name in targets]
    discovered = [name for name in ordered if shutil.which(name)]
    return discovered or ordered


def render(enabled) -> str:
    enabled = [name for name in RUNTIMES if name in enabled]
    if not enabled:
        raise ValueError("routing config requires at least one enabled runtime")
    peers = [name for name in enabled if name != "opencode"]
    opencode = ["opencode"] if "opencode" in enabled else []
    if not peers:
        raise ValueError(
            "routing config requires at least one quality-peer runtime (claude or codex)"
        )

    def inline(values):
        return "[" + ", ".join(values) + "]"

    lines = [
        "# User-owned Hearting routing policy. Install/update never overwrites this file.",
        "schema_version: 3",
        "harnesses:",
        f"  enabled: {inline(enabled)}",
        "profiles:",
    ]
    for profile in ("deep", "balanced-deep", "light", "mini"):
        light = profile in {"light", "mini"}
        lines += [
            f"  {profile}:",
            f"    primary: {inline(peers + opencode if light else peers)}",
            f"    relief: {inline([])}",
            f"    last_resort: {inline([] if light else opencode)}",
            f"    promote_relief_below: 0",
        ]
    lines += [
        "allocation:",
        "  strategy: balanced",
        "  window: 30",
        "  usage_gate_used_percent: 85",
        "  depth_affinity:",
        *[f"    {depth}: {harness}" for depth, harness in (("owner", "claude"), ("worker", "codex")) if harness in enabled],
        "  depth_affinity_weight: 0.65",
        "  usage_headroom_exponent: 2",
        # Omitted cells inherit the shipped profiles/dispatch-defaults.yaml
        # capability baseline; a cell written here always wins over it.
        "capabilities:",
        "",
    ]
    return "\n".join(lines)


def ensure(targets, *, dry_run=False) -> dict:
    path = config_path()
    enabled = available_runtimes(targets)
    if path.exists():
        return {"status": "preserved", "path": str(path), "enabled": enabled}
    if not any(name in enabled for name in ("claude", "codex")):
        return {
            "status": "skipped-no-quality-peer",
            "path": str(path),
            "enabled": enabled,
        }
    if dry_run:
        return {"status": "would-create", "path": str(path), "enabled": enabled}
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    try:
        fd = os.open(path, flags, 0o600)
    except FileExistsError:
        return {"status": "preserved", "path": str(path), "enabled": enabled}
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(render(enabled))
    return {"status": "created", "path": str(path), "enabled": enabled}


def validate() -> dict:
    path = config_path()
    if not path.is_file():
        return {"status": "missing", "ok": False, "path": str(path), "detail": "not initialized"}
    result = subprocess.run(
        [sys.executable, str(Path(__file__).resolve().parents[2] / "utilities" / "dispatch-defaults.py"),
         "validate", "--config", str(path)],
        text=True,
        capture_output=True,
        check=False,
    )
    detail = (result.stdout or result.stderr).strip()
    if result.returncode != 0:
        return {"status": "invalid", "ok": False, "path": str(path), "detail": detail}
    # `warning=` lines are the validator's drift findings: a legacy strategy
    # that never adopted the shipped default, or keys the strategy ignores.
    # Still ok (the file is valid) but surfaced as a distinct status so
    # install, `harness verify`, and `harness config status` all show it.
    warnings = [
        line[len("warning="):].strip()
        for line in result.stdout.splitlines()
        if line.startswith("warning=")
    ]
    if warnings:
        return {
            "status": "drift",
            "ok": True,
            "path": str(path),
            "detail": "; ".join(warnings),
            "warnings": warnings,
        }
    return {"status": "valid", "ok": True, "path": str(path), "detail": detail, "warnings": []}
