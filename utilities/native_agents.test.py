#!/usr/bin/env python3
"""Tests for the shared native agent renderer and its three adapter formatters."""

from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "utilities"))

import model_profile  # noqa: E402
import model_config  # noqa: E402
import native_agents  # noqa: E402

ADAPTERS = ("claude", "codex", "opencode")
# Tier keys a user copy edits so every generated agent of the adapter changes model.
DEEP_KEYS = {
    "claude": ("CFG_TIER_DEEP_MODEL", "CFG_TIER_BALANCED_DEEP_MODEL"),
    "codex": ("CFG_TIER_DEEP_MODEL",),
    "opencode": ("CFG_TIER_BALANCED_DEEP_MODEL",),
}
LIGHT_KEYS = {a: ("CFG_TIER_LIGHT_MODEL",) for a in ADAPTERS}


def load_formatter(adapter: str):
    script = ROOT / "adapters" / adapter / "bin" / "sync-native-agents.py"
    spec = importlib.util.spec_from_file_location(f"{adapter}_native_agents_fmt", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def shipped_cfg(adapter: str) -> dict[str, str]:
    return model_profile.load_config(ROOT / "adapters" / adapter / "config" / "models.conf")


def write_user_conf(home: Path, adapter: str, edits: dict[str, str]) -> Path:
    """Write a complete user copy: the shipped file with `edits` applied."""
    text = (ROOT / "adapters" / adapter / "config" / "models.conf").read_text(encoding="utf-8")
    for key, value in edits.items():
        line = f'{key}="{value}"' if " " in value or not value else f"{key}={value}"
        pattern = re.compile(rf"^{key}=.*$", re.M)
        if pattern.search(text):
            text = pattern.sub(lambda _m: line, text, count=1)
        else:
            text += f"\n{line}\n"
    target = home / "agent-config" / "models.conf"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return target


def write_legacy_user_conf(home: Path, adapter: str) -> Path:
    text = (ROOT / "adapters" / adapter / "config" / "models.conf").read_text(encoding="utf-8")
    text = re.sub(r"^CFG_MODEL_PROFILE_(?:BALANCED|GRANULARITY_BALANCED)=.*\n", "", text, flags=re.M)
    text = re.sub(r"\s*balanced:balanced(?=\s|\")", "", text)
    target = home / "agent-config" / "models.conf"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return target


def run_plan(adapter: str, home: Path) -> dict:
    out = subprocess.run(
        [sys.executable, str(ROOT / "adapters" / adapter / "bin" / "sync-native-agents.py"),
         "--runtime-home", str(home), "--json"],
        check=True, capture_output=True, text=True)
    return json.loads(out.stdout)


def by_name(plan: dict) -> dict[str, dict]:
    return {row["name"]: row for row in plan["agents"]}


class SharedRendererTest(unittest.TestCase):
    def test_shipped_render_matches_committed_files_and_parse_is_stable(self):
        for adapter in ADAPTERS:
            with self.subTest(adapter):
                fmt = load_formatter(adapter).FORMAT
                agents = native_agents.plan_agents(fmt, shipped_cfg(adapter))
                self.assertTrue(agents)
                for agent in agents:
                    self.assertEqual(agent.status, "render", agent)
                    committed = fmt.out_dir / agent.shipped_relpath
                    self.assertEqual(committed.read_text(encoding="utf-8"), agent.body, agent.name)
                self.assertEqual(
                    subprocess.run([sys.executable, str(ROOT / "adapters" / adapter / "bin" / "sync-native-agents.py"),
                                    "--check"], capture_output=True).returncode, 0)

    def test_runtime_plan_with_shipped_config_is_all_shipped(self):
        # Guards against a false `rendered` from any parser difference between
        # the generator's loader and the runtime config selector.
        for adapter in ADAPTERS:
            with self.subTest(adapter), tempfile.TemporaryDirectory() as tmp:
                plan = run_plan(adapter, Path(tmp))
                self.assertEqual(plan["schema"], native_agents.SCHEMA)
                self.assertEqual(plan["config"]["source"], "shipped")
                self.assertEqual({row["status"] for row in plan["agents"]}, {"shipped"})
                copy = Path(tmp)
                write_user_conf(copy, adapter, {})
                plan = run_plan(adapter, copy)
                self.assertEqual(plan["config"]["source"], "user")
                self.assertEqual({row["status"] for row in plan["agents"]}, {"shipped"})

    def test_legacy_user_copy_keeps_balanced_with_the_derived_model(self):
        for adapter in ADAPTERS:
            with self.subTest(adapter), tempfile.TemporaryDirectory() as tmp:
                home = Path(tmp)
                write_legacy_user_conf(home, adapter)
                cfg, receipt = model_config.resolve_config(adapter, runtime=home, source_root=ROOT)
                self.assertEqual(receipt.reason, "user-valid-derived-balanced")
                expected_model = model_profile.resolve_profile_values(adapter, cfg, "balanced")["model"]
                rows = by_name(run_plan(adapter, home))
                self.assertIn("balanced", rows)
                self.assertEqual(rows["balanced"]["model"], expected_model)

    def test_explicit_balanced_profile_respects_catalog_omission(self):
        for adapter in ADAPTERS:
            with self.subTest(adapter), tempfile.TemporaryDirectory() as tmp:
                home = Path(tmp)
                write_user_conf(home, adapter, {
                    "CFG_NATIVE_AGENT_CATALOG": "general-purpose:balanced-deep light:light deep:deep"
                })
                rows = by_name(run_plan(adapter, home))
                self.assertNotIn("balanced", rows)

    def test_codex_one_argument_call_keeps_shipped_meaning(self):
        codex = load_formatter("codex")
        cfg = shipped_cfg("codex")
        self.assertEqual(codex.codex_config("memory-scout"), codex.codex_config("memory-scout", cfg))
        model, effort, sandbox = codex.codex_config("memory-scout")
        self.assertEqual((effort, sandbox), ("low", "read-only"))
        self.assertEqual(model, cfg["CFG_TIER_LIGHT_MODEL"])
        user = dict(cfg, CFG_TIER_LIGHT_MODEL="fixture-user-light")
        self.assertEqual(codex.codex_config("memory-scout", user)[0], "fixture-user-light")

    def test_catalog_parser_rejects_malformed_top_and_empty_for_the_shipped_catalog(self):
        for bad in ("deep", "a:b:c", "deep:top"):
            with self.subTest(bad), self.assertRaises(SystemExit):
                native_agents.parse_catalog({"CFG_NATIVE_AGENT_CATALOG": bad})
        with self.assertRaises(SystemExit):
            native_agents.parse_catalog({"CFG_NATIVE_AGENT_CATALOG": ""})
        self.assertEqual(native_agents.parse_catalog({"CFG_NATIVE_AGENT_CATALOG": "deep:deep light:light"}),
                         [("deep", "deep"), ("light", "light")])
        # The runtime form never aborts: a bad token is reported and the rest renders.
        entries, problems = native_agents.catalog_tokens({"CFG_NATIVE_AGENT_CATALOG": "deep:deep bad deep:top"})
        self.assertEqual(entries, [("deep", "deep")])
        self.assertEqual(len(problems), 2)

    def test_every_model_main_session_only_withholds_every_agent(self):
        # Known defect 1: an empty projection must be a normal result, not "inactive".
        for adapter in ("codex", "claude", "opencode"):
            with self.subTest(adapter), tempfile.TemporaryDirectory() as tmp:
                fmt = load_formatter(adapter).FORMAT
                models = sorted({a.model for a in native_agents.plan_agents(fmt, shipped_cfg(adapter)) if a.model})
                home = Path(tmp)
                write_user_conf(home, adapter, {"CFG_MAIN_SESSION_ONLY_MODELS": " ".join(models)})
                plan = run_plan(adapter, home)
                rows = by_name(plan)
                self.assertTrue(plan["agents"])
                for row in plan["agents"]:
                    if row["model"] is None:
                        # OpenCode's memory-scout pins no model and cannot be withheld.
                        self.assertEqual(row["name"], "memory-scout")
                        self.assertEqual(row["status"], "shipped")
                    else:
                        self.assertEqual(row["status"], "withheld", row)
                        self.assertIn("main-session-only", row["reason"])
                self.assertIn("deep", rows)

    def test_user_models_render_with_the_user_model_in_the_content(self):
        for adapter in ADAPTERS:
            with self.subTest(adapter), tempfile.TemporaryDirectory() as tmp:
                home = Path(tmp)
                edits = {key: "fixture-user-deep" for key in DEEP_KEYS[adapter]}
                edits.update({key: "fixture-user-light" for key in LIGHT_KEYS[adapter]})
                write_user_conf(home, adapter, edits)
                plan = run_plan(adapter, home)
                self.assertEqual(plan["config"]["source"], "user")
                for row in plan["agents"]:
                    if row["model"] is None:
                        continue
                    self.assertEqual(row["status"], "rendered", row)
                    self.assertIn(row["model"], {"fixture-user-deep", "fixture-user-light"}, row)
                    self.assertIn(row["model"], row["content"])
                # The shipped files on disk are never modified by planning.
                self.assertEqual(subprocess.run(
                    [sys.executable, str(ROOT / "adapters" / adapter / "bin" / "sync-native-agents.py"), "--check"],
                    capture_output=True).returncode, 0)

    def test_catalog_names_without_a_spec_are_skipped_and_removed_names_are_absent(self):
        for adapter in ADAPTERS:
            with self.subTest(adapter), tempfile.TemporaryDirectory() as tmp:
                home = Path(tmp)
                write_user_conf(home, adapter, {"CFG_NATIVE_AGENT_CATALOG": "deep:deep ghost:light"})
                rows = by_name(run_plan(adapter, home))
                self.assertEqual(rows["ghost"]["status"], "skipped")
                self.assertEqual(rows["deep"]["status"], "shipped")
                for gone in ("light", "balanced", "general-purpose"):
                    self.assertNotIn(gone, rows)

    def test_an_empty_catalog_plans_only_the_kernel_helpers(self):
        # A user copy cannot declare an empty catalog (an empty value makes the
        # file malformed and the shipped file is selected), but the planner
        # itself treats an empty catalog as a normal result, not an error.
        for adapter in ADAPTERS:
            with self.subTest(adapter):
                fmt = load_formatter(adapter).FORMAT
                agents = native_agents.plan_agents(fmt, dict(shipped_cfg(adapter), CFG_NATIVE_AGENT_CATALOG=""))
                self.assertEqual([a.name for a in agents], list(fmt.kernel_names))

    def test_static_names_are_declared_for_hand_written_files(self):
        self.assertEqual(load_formatter("claude").FORMAT.static_names, ("memory-scout",))
        self.assertEqual(load_formatter("codex").FORMAT.static_names, ())
        self.assertEqual(load_formatter("opencode").FORMAT.static_names, ())


if __name__ == "__main__":
    unittest.main(verbosity=2)
