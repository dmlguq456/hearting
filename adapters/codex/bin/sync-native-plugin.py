#!/usr/bin/env python3
"""Generate the Codex-native plugin projection for the portable harness."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

# S-5d (owner-supervisor-liveness, core/OPERATIONS.md §5.10): same generated
# JSON mode invariant as adapters/claude/bin/sync-native-plugin.py.
JSON_MODE = 0o644


ROOT = Path(__file__).resolve().parents[3]
ADAPTER = ROOT / "adapters" / "codex"
PLUGIN_NAME = "hearting-codex"
PLUGIN_VERSION = "0.1.0+codex.20260715015036"
PLUGIN_ROOT = ADAPTER / "plugins" / PLUGIN_NAME
MARKETPLACE_ROOT = ADAPTER / "plugin-marketplace"
MARKETPLACE = MARKETPLACE_ROOT / ".agents" / "plugins" / "marketplace.json"
MARKETPLACE_PLUGIN_LINK = MARKETPLACE_ROOT / "plugins" / PLUGIN_NAME
MARKETPLACE_PLUGIN_TARGET = Path("../../plugins") / PLUGIN_NAME
SKILLS = ADAPTER / "skills"
VALIDATOR = Path.home() / ".codex" / "skills" / ".system" / "plugin-creator" / "scripts" / "validate_plugin.py"


def plugin_json() -> dict:
    return {
        "name": PLUGIN_NAME,
        "version": PLUGIN_VERSION,
        "description": "Codex-native plugin projection for the portable agent harness.",
        "author": {
            "name": "hearting",
        },
        "skills": "./skills/",
        "interface": {
            "displayName": "Hearting Codex",
            "shortDescription": "Portable agent harness capabilities for Codex.",
            "longDescription": (
                "Adapter-owned Codex plugin projection generated from portable "
                "Hearting capability contracts. Legacy runtime files are reference only."
            ),
            "developerName": "hearting",
            "category": "Developer Tools",
            "capabilities": ["Interactive", "Write"],
            "defaultPrompt": [
                "Use the portable agent harness in Codex.",
            ],
        },
    }


def marketplace_json() -> dict:
    return {
        "name": "hearting",
        "interface": {
            "displayName": "Hearting",
        },
        "plugins": [
            {
                "name": PLUGIN_NAME,
                "source": {
                    "source": "local",
                    "path": f"./plugins/{PLUGIN_NAME}",
                },
                "policy": {
                    "installation": "AVAILABLE",
                    "authentication": "ON_INSTALL",
                },
                "category": "Developer Tools",
            }
        ],
    }


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
        ) as handle:
            temporary = Path(handle.name)
            handle.write(json.dumps(payload, indent=2) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(JSON_MODE)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


def sync() -> None:
    if not SKILLS.exists():
        raise SystemExit("Codex native skills are missing; run adapters/codex/bin/sync-native-skills.py first")

    write_json(PLUGIN_ROOT / ".codex-plugin" / "plugin.json", plugin_json())

    plugin_skills = PLUGIN_ROOT / "skills"
    if plugin_skills.exists() or plugin_skills.is_symlink():
        shutil.rmtree(plugin_skills)
    shutil.copytree(SKILLS, plugin_skills)

    write_json(MARKETPLACE, marketplace_json())
    MARKETPLACE_PLUGIN_LINK.parent.mkdir(parents=True, exist_ok=True)
    if MARKETPLACE_PLUGIN_LINK.is_symlink() or MARKETPLACE_PLUGIN_LINK.exists():
        if MARKETPLACE_PLUGIN_LINK.is_dir() and not MARKETPLACE_PLUGIN_LINK.is_symlink():
            shutil.rmtree(MARKETPLACE_PLUGIN_LINK)
        else:
            MARKETPLACE_PLUGIN_LINK.unlink()
    MARKETPLACE_PLUGIN_LINK.symlink_to(MARKETPLACE_PLUGIN_TARGET)


def check_file(path: Path, expected: str, stale: list[str]) -> None:
    if not path.exists() or path.read_text(encoding="utf-8") != expected:
        stale.append(str(path.relative_to(ROOT)))
        return
    # Git tracks only executable bits for regular files; group-write is a
    # checkout umask/mount detail that a commit cannot normalize.
    if stat.S_IMODE(path.stat().st_mode) & 0o111:
        stale.append(str(path.relative_to(ROOT)) + " (mode)")


def check() -> int:
    stale: list[str] = []
    check_file(
        PLUGIN_ROOT / ".codex-plugin" / "plugin.json",
        json.dumps(plugin_json(), indent=2) + "\n",
        stale,
    )
    check_file(MARKETPLACE, json.dumps(marketplace_json(), indent=2) + "\n", stale)
    if (ADAPTER / ".agents").exists():
        stale.append(str((ADAPTER / ".agents").relative_to(ROOT)))
    if not MARKETPLACE_PLUGIN_LINK.is_symlink():
        stale.append(str(MARKETPLACE_PLUGIN_LINK.relative_to(ROOT)))
    elif os.readlink(MARKETPLACE_PLUGIN_LINK) != str(MARKETPLACE_PLUGIN_TARGET):
        stale.append(str(MARKETPLACE_PLUGIN_LINK.relative_to(ROOT)))

    for skill in sorted(SKILLS.glob("*/SKILL.md")):
        rel = skill.relative_to(SKILLS)
        plugin_skill = PLUGIN_ROOT / "skills" / rel
        if not plugin_skill.exists() or plugin_skill.read_text(encoding="utf-8") != skill.read_text(encoding="utf-8"):
            stale.append(str(plugin_skill.relative_to(ROOT)))

    expected = {PLUGIN_ROOT / "skills" / skill.relative_to(SKILLS) for skill in SKILLS.glob("*/SKILL.md")}
    plugin_skill_files = sorted((PLUGIN_ROOT / "skills").glob("*/SKILL.md")) if (PLUGIN_ROOT / "skills").exists() else []
    for path in plugin_skill_files:
        if path not in expected:
            stale.append(str(path.relative_to(ROOT)))

    if VALIDATOR.exists():
        result = subprocess.run(
            [sys.executable, str(VALIDATOR), str(PLUGIN_ROOT)],
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )
        if result.returncode != 0:
            print(result.stdout, file=sys.stderr)
            stale.append(str(PLUGIN_ROOT.relative_to(ROOT)))

    if stale:
        print("Codex native plugin projection is stale:", file=sys.stderr)
        for item in stale:
            print(f"  {item}", file=sys.stderr)
        return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="verify generated projection")
    args = parser.parse_args()

    if args.check:
        return check()
    sync()
    print(f"generated Codex native plugin projection at {PLUGIN_ROOT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
