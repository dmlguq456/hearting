#!/usr/bin/env python3
"""Shared UI text: language selection follows the existing preference path."""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("ui_text", HERE / "ui_text.py")
UI = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(UI)

PLACEHOLDER = __import__("re").compile(r"\{(\w+)\}")


class CatalogShape(unittest.TestCase):
    def test_every_key_has_both_languages_with_the_same_fields(self):
        messages = json.loads((HERE / "messages.json").read_text(encoding="utf-8"))["messages"]
        self.assertTrue(messages)
        for key, entry in messages.items():
            with self.subTest(key=key):
                self.assertEqual(set(entry), {"en", "ko"})
                self.assertEqual(set(PLACEHOLDER.findall(entry["en"])),
                                 set(PLACEHOLDER.findall(entry["ko"])))

    def test_keys_are_sorted_so_merges_stay_small(self):
        messages = json.loads((HERE / "messages.json").read_text(encoding="utf-8"))["messages"]
        self.assertEqual(list(messages), sorted(messages))


class Resolution(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.env = {"HOME": str(root / "home"), "XDG_CONFIG_HOME": str(root / "xdg"),
                    "CLAUDE_CONFIG_DIR": str(root / "claude")}
        for key in ("FLEET_NOW_LANG",):
            os.environ.pop(key, None)

    def _run(self, **extra):
        env = {**self.env, **extra}
        return mock.patch.dict(os.environ, env, clear=False)

    def _claude(self, language):
        path = Path(self.env["CLAUDE_CONFIG_DIR"]) / "settings.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"language": language}), encoding="utf-8")

    def _fleet(self, language):
        path = Path(self.env["XDG_CONFIG_HOME"]) / "hearting" / "fleet.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"title_language": language}), encoding="utf-8")

    def test_no_declaration_is_english(self):
        with self._run(FLEET_NOW_LANG=""):
            self.assertEqual(UI.language(), "en")
            self.assertEqual(UI.translator()("route.campaign.unassigned"),
                             "No campaign assigned (_unassigned, degraded)")

    def test_claude_language_selects_korean_without_any_new_setting(self):
        self._claude("korean")
        with self._run(FLEET_NOW_LANG=""):
            self.assertEqual(UI.language(), "ko")
            self.assertEqual(UI.translator()("route.campaign.unassigned"),
                             "캠페인 미배정 (_unassigned, degraded)")

    def test_order_is_env_then_fleet_json_then_claude(self):
        self._claude("korean")
        self._fleet("English")
        with self._run(FLEET_NOW_LANG=""):
            self.assertEqual(UI.language(), "en")  # fleet.json beats Claude
        with self._run(FLEET_NOW_LANG="Korean"):
            self.assertEqual(UI.language(), "ko")  # env beats fleet.json

    def test_names_and_codes(self):
        for name in ("ko", "KO", "ko-KR", "ko_KR", "Korean", "korean", "한국어"):
            with self.subTest(name=name):
                self.assertEqual(UI.normalize(name), "ko")
        for name in ("", "auto", "en", "English", "Japanese", "kotlin", None):
            with self.subTest(name=name):
                self.assertEqual(UI.normalize(name), "en")

    def test_unreadable_preference_never_blocks(self):
        Path(self.env["XDG_CONFIG_HOME"], "hearting").mkdir(parents=True)
        Path(self.env["XDG_CONFIG_HOME"], "hearting", "fleet.json").write_text("{not json")
        with self._run(FLEET_NOW_LANG=""):
            self.assertEqual(UI.language(), "en")

    def test_a_field_may_be_called_key(self):
        # Card fields are often named `key`; the message id is positional-only.
        self.assertEqual(UI.text("route.campaign.create", "en", key="k1"), "Campaign k1 (new)")
        self.assertEqual(UI.translator("ko")("route.campaign.create", key="k1"), "캠페인 k1 (신규 생성)")

    def test_missing_key_is_loud_and_fields_fill(self):
        with self.assertRaises(KeyError):
            UI.text("no.such.key")
        self.assertEqual(UI.text("route.spec.shared", "ko", path="/x"),
                         "공유 spec 있음 — 읽을 경로: /x")
        self.assertEqual(UI.text("route.spec.shared", "en", path="/x"),
                         "Shared spec available — read: /x")


class Cli(unittest.TestCase):
    def run_cli(self, *args):
        env = {k: v for k, v in os.environ.items() if k != "FLEET_NOW_LANG"}
        return subprocess.run([sys.executable, str(HERE / "ui_text.py"), *args],
                              text=True, capture_output=True, env=env)

    def test_shell_and_js_read_the_same_catalog(self):
        ko = self.run_cli("--lang", "ko", "get", "route.spec.shared", "path=/p")
        en = self.run_cli("--lang", "en", "get", "route.spec.shared", "path=/p")
        self.assertEqual((ko.returncode, ko.stdout), (0, "공유 spec 있음 — 읽을 경로: /p\n"))
        self.assertEqual((en.returncode, en.stdout), (0, "Shared spec available — read: /p\n"))
        self.assertEqual(self.run_cli("--lang", "ko", "lang").stdout, "ko\n")
        self.assertEqual(self.run_cli("get", "missing.key").returncode, 2)


if __name__ == "__main__":
    unittest.main()
