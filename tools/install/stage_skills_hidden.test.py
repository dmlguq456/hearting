#!/usr/bin/env python3
"""Stage-only (parent-invoked) skills never reach the main session's native skill list."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

import harness_manifest  # noqa: E402
import projector  # noqa: E402
import runtime_activation  # noqa: E402

ROOT = HERE.parent.parent
MANIFEST = json.loads((ROOT / "harness-manifest.json").read_text(encoding="utf-8"))
STAGE = {n for n, s in MANIFEST["capabilities"].items()
         if s["invocation"]["class"] in ("parent-invoked", "compiler-internal")}
VISIBLE = {n for n, s in MANIFEST["capabilities"].items()
           if s["invocation"]["class"] not in ("parent-invoked", "compiler-internal")}
ENTRY = {n for n, s in MANIFEST["capabilities"].items() if s["invocation"]["class"] == "entry-router"}
SUPPORT = {n for n, s in MANIFEST["capabilities"].items() if s["invocation"]["class"] == "model-support"}


def _skill_names(entries, home):
    skills = home / "skills"
    return {Path(e["dest"]).name for e in entries
            if e.get("dest") and Path(e["dest"]).parent == skills}


class StageSkillsHiddenTest(unittest.TestCase):
    def test_rule_covers_thirteen_stage_skills(self):
        self.assertEqual(harness_manifest.main_hidden_skill_names(MANIFEST), STAGE)
        self.assertEqual(len([n for n in STAGE if MANIFEST["capabilities"][n]["invocation"]["class"] == "parent-invoked"]), 13)

    def test_stage_skill_files_still_generated(self):
        for runtime in ("claude", "codex", "opencode"):
            for name in STAGE - {"route-frame"}:
                self.assertTrue((ROOT / "adapters" / runtime / "skills" / name / "SKILL.md").is_file(), (runtime, name))

    def test_activation_projection_excludes_stage_skills(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {
                "CODEX_HOME": f"{tmp}/codex", "CLAUDE_CONFIG_DIR": f"{tmp}/claude",
                "XDG_CONFIG_HOME": f"{tmp}/xdg", "AGENT_HOME": str(ROOT)}):
            for runtime in ("claude", "codex", "opencode"):
                home = runtime_activation.paths.runtime_home(runtime)
                names = _skill_names(runtime_activation._linked_entries(runtime, ROOT), home)
                # OpenCode has no explicit-only switch: entry routers leave its projection.
                self.assertEqual(names, VISIBLE - ENTRY if runtime == "opencode" else VISIBLE, runtime)

    def test_installer_projection_excludes_stage_skills(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {
                "CODEX_HOME": f"{tmp}/codex", "CLAUDE_CONFIG_DIR": f"{tmp}/claude",
                "AGENT_HOME": str(ROOT)}):
            for runtime in ("claude", "codex", "opencode"):
                home = projector.paths.runtime_home(runtime)
                names = _skill_names(projector.plan([runtime])[runtime], home)
                self.assertEqual(names, VISIBLE - ENTRY if runtime == "opencode" else VISIBLE, runtime)

    def test_native_plugin_copies_exclude_stage_skills(self):
        for plugin in ("adapters/codex/plugins/hearting-codex/skills",
                       "adapters/claude/plugin-marketplace/plugins/hearting-claude/skills"):
            present = {p.name for p in (ROOT / plugin).iterdir() if p.is_dir()}
            self.assertEqual(present & STAGE, set(), plugin)

    def test_entry_routers_are_explicit_only(self):
        self.assertEqual(harness_manifest.explicit_only_skill_names(MANIFEST), ENTRY)
        self.assertEqual(len(ENTRY), 12)
        self.assertEqual(ENTRY & STAGE, set())
        for name in ENTRY:
            codex = ROOT / "adapters/codex/skills" / name / "agents/openai.yaml"
            self.assertIn("allow_implicit_invocation: false", codex.read_text(), name)
            claude = (ROOT / "adapters/claude/skills" / name / "SKILL.md").read_text()
            self.assertIn("\ndisable-model-invocation: true\n", claude.split("\n---", 2)[0], name)
            self.assertTrue((ROOT / "adapters/opencode/commands" / f"{name}.md").is_file(), name)
        for name in SUPPORT:
            self.assertFalse((ROOT / "adapters/codex/skills" / name / "agents").exists(), name)
            self.assertNotIn("disable-model-invocation",
                             (ROOT / "adapters/claude/skills" / name / "SKILL.md").read_text(), name)

    def test_codex_plugin_copies_carry_the_policy(self):
        for name in ENTRY:
            self.assertTrue((ROOT / "adapters/codex/plugins/hearting-codex/skills" / name
                             / "agents/openai.yaml").is_file(), name)


if __name__ == "__main__":
    unittest.main()
