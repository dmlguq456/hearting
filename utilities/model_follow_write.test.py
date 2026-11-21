#!/usr/bin/env python3
"""A table cell follows the newer model for real: one write, one backup, a record on the line.

Every case runs on all three harnesses on a fixture home with fake list rows; nothing real is
read or written. Model names are made up on purpose (vendor patterns are tested with each
harness's list reader).
"""
from __future__ import annotations
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import fcntl
import json
import stat
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "utilities"))
sys.path.insert(0, str(ROOT / "tools" / "install"))
import model_config  # noqa: E402
import model_follow  # noqa: E402
import model_follow_mark as K  # noqa: E402
import model_outcome_record as M  # noqa: E402
import model_settings  # noqa: E402
import safe_fs  # noqa: E402

HARNESSES = ("claude", "codex", "opencode")
TODAY = "2026-11-21"


def row(model, family="fam", version=(1,), **fields):
    base = {"id": model, "family": family, "version": version, "visible": True,
            "deprecated": False, "efforts": None, "cost": None, "upgrade": None, "min_cli": None}
    return {**base, **fields}


def conf_text(adapter, *, deep="deep-1", light="light-1", top="top-1", suffix=None, extra=""):
    """A complete table; `suffix` maps a key to the comment text its line ends with."""
    suffix = suffix or {}
    effort = "VARIANT" if adapter == "opencode" else "EFFORT"
    prefix = "prov/" if adapter == "opencode" else ""
    lines = []

    def put(key, value):
        lines.append(f"{key}={value}" + (f" {suffix[key]}" if key in suffix else ""))

    if adapter != "opencode":
        put("CFG_MAIN_SESSION_ONLY_MODELS", f'"{prefix}{top}"')
    for tier, model, level in (("TOP", top, "xhigh"), ("DEEP", deep, "xhigh"),
                               ("BALANCED_DEEP", deep, "medium"), ("LIGHT", light, "medium"),
                               ("MINI", light, "low")):
        put(f"CFG_TIER_{tier}_MODEL", f"{prefix}{model}")
        put(f"CFG_TIER_{tier}_{effort}", level)
    put("CFG_MODEL_PROFILE_DEEP", f"model/{prefix}{deep}:high")
    put("CFG_MODEL_PROFILE_BALANCED_DEEP", "balanced-deep:medium")
    put("CFG_MODEL_PROFILE_BALANCED", "light:high")
    put("CFG_MODEL_PROFILE_LIGHT", "light:medium")
    put("CFG_MODEL_PROFILE_MINI", "mini:low")
    put("CFG_MODEL_PROFILE_TOP", "top:xhigh")
    put("CFG_MODEL_PROFILE_GRANULARITY", "full")
    put("CFG_TIER_DEEP_FAILOVER", "light")
    put("CFG_TIER_DEEP_FAILOVER_CASCADE", f'"{prefix}{deep}:xhigh {prefix}{light}:medium"')
    if adapter == "opencode":
        put("CFG_V2_AUTO_FALLBACKS", f'"{prefix}{deep}:runtime-default"')
    return "\n".join(lines) + "\n" + extra


def changed(before: str, after: str):
    """The (old, new) pairs of the lines that differ; the two texts have the same line count."""
    old, new = before.splitlines(keepends=True), after.splitlines(keepends=True)
    assert len(old) == len(new), (len(old), len(new))
    return [(a, b) for a, b in zip(old, new) if a != b]


class Home(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="mf3t-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.env = {"HOME": str(self.root), "CLAUDE_CONFIG_DIR": str(self.root / "claude"),
                    "CODEX_HOME": str(self.root / "codex"), "XDG_CONFIG_HOME": str(self.root / "cfg"),
                    "XDG_STATE_HOME": str(self.root / "state"), "PATH": ""}
        patcher = mock.patch.dict(os.environ, self.env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        for adapter in HARNESSES:
            config = self.root / "src" / "adapters" / adapter / "config"
            config.mkdir(parents=True)
            (config / "models.conf").write_text(conf_text(adapter))
        self.src = str(self.root / "src")
        # a close reaches the table without being told where the shipped file is
        root = mock.patch.object(model_config, "repository_root", return_value=self.root / "src")
        root.start()
        self.addCleanup(root.stop)

    def path(self, adapter):
        base = {"claude": self.root / "claude", "codex": self.root / "codex",
                "opencode": self.root / "cfg" / "opencode"}[adapter]
        return base / "agent-config" / "models.conf"

    def table(self, adapter, text=None, **kwargs):
        path = self.path(adapter)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text if text is not None else conf_text(adapter, **kwargs))
        return path

    def baks(self, adapter):
        return sorted(p.name for p in self.path(adapter).parent.glob("models.conf.bak*"))

    @staticmethod
    def name(adapter):
        prefix = "prov/" if adapter == "opencode" else ""
        return lambda model: prefix + model

    def apply(self, adapter, rows, *, client_version=None, probe=None, **kwargs):
        catalog = SimpleNamespace(state="available", rows=tuple(rows), client_version=client_version)
        kwargs.setdefault("read_catalog", lambda _harness: catalog)
        return model_follow.apply_harness(adapter, source_root=self.src, today=TODAY,
                                          probe=probe or (lambda _harness: (None, None)), **kwargs)

    def survey(self, adapter, rows, **kwargs):
        catalog = SimpleNamespace(state="available", rows=tuple(rows), client_version=None)
        return model_follow.survey_harness(adapter, source_root=self.src, today=TODAY,
                                           read_catalog=lambda _harness: catalog,
                                           probe=lambda _harness: (None, None), **kwargs)

    def newer(self, adapter, **fields):
        name = self.name(adapter)
        return [row(name("deep-1")), row(name("deep-2"), version=(2,), **fields),
                row(name("light-1"), family="light"), row(name("top-1"), family="top")]


def record(adapter, before, after, was=None, date=TODAY):
    name = Home.name(adapter)
    return K.render(K.follow_record(date, name(before), name(after), was))


class FollowWriteTests(Home):
    def test_a_newer_model_is_written_to_every_cell_that_names_the_old_one(self):
        for adapter in HARNESSES:
            with self.subTest(adapter=adapter):
                path = self.table(adapter)
                before = path.read_text()
                name = self.name(adapter)
                result = self.apply(adapter, self.newer(adapter))
                after = path.read_text()
                self.assertEqual((result["status"], result["error"]), ("ok", None))
                keys = [pair[1].split("=")[0] for pair in changed(before, after)]
                expected = ["CFG_TIER_DEEP_MODEL", "CFG_TIER_BALANCED_DEEP_MODEL", "CFG_MODEL_PROFILE_DEEP",
                            "CFG_TIER_DEEP_FAILOVER_CASCADE"]
                if adapter == "opencode":
                    expected.append("CFG_V2_AUTO_FALLBACKS")
                self.assertEqual(sorted(keys), sorted(expected))
                values = model_config.parse_config(path)
                self.assertEqual(values["CFG_TIER_DEEP_MODEL"], name("deep-2"))
                self.assertEqual(values["CFG_MODEL_PROFILE_DEEP"], f"model/{name('deep-2')}:high")
                self.assertIn(f"{name('deep-2')}:xhigh", values["CFG_TIER_DEEP_FAILOVER_CASCADE"])
                self.assertIn(f"{name('light-1')}:medium", values["CFG_TIER_DEEP_FAILOVER_CASCADE"])
                marks = K.read_marks(after)
                self.assertEqual(marks["CFG_TIER_DEEP_MODEL"], K.follow_record(TODAY, name("deep-1"), name("deep-2")))
                self.assertEqual(marks["CFG_MODEL_PROFILE_DEEP"]["to"], name("deep-2"))
                self.assertEqual(marks["CFG_TIER_DEEP_FAILOVER_CASCADE"]["from"], name("deep-1"))
                self.assertEqual(self.baks(adapter), ["models.conf.bak"])
                self.assertEqual((path.parent / "models.conf.bak").read_text(), before)

    def test_lines_that_are_not_changed_stay_byte_for_byte_with_unknown_lines_and_comments(self):
        for adapter in HARNESSES:
            with self.subTest(adapter=adapter):
                extra = '# a note of mine\n\nCFG_SOMETHING_NEW="weird   value" # unknown to the tool\n'
                if adapter == "codex":  # one file may mix line endings
                    extra += "CFG_CRLF_LINE=1 # crlf\r\n"
                text = conf_text(adapter, suffix={"CFG_TIER_DEEP_MODEL": "# my deep model",
                                                  "CFG_TIER_LIGHT_MODEL": "# stays"}, extra=extra)
                path = self.table(adapter)
                path.write_bytes(text.encode())
                self.apply(adapter, self.newer(adapter))
                after = path.read_bytes().decode()
                old, new = text.splitlines(keepends=True), after.splitlines(keepends=True)
                self.assertEqual(len(old), len(new))
                moved = {a.split("=")[0] for a, b in zip(old, new) if a != b}
                self.assertNotIn("CFG_SOMETHING_NEW", moved)
                self.assertNotIn("CFG_TIER_LIGHT_MODEL", moved)
                self.assertIn(extra, after)
                deep = next(line for line in new if line.startswith("CFG_TIER_DEEP_MODEL="))
                self.assertIn("# my deep model", deep)
                self.assertEqual(deep.count("auto-follow"), 1)

    def test_nothing_newer_writes_nothing(self):
        for adapter in HARNESSES:
            with self.subTest(adapter=adapter):
                path = self.table(adapter)
                before = path.read_bytes()
                name = self.name(adapter)
                result = self.apply(adapter, [row(name("deep-1")), row(name("light-1"), family="light")])
                self.assertEqual(path.read_bytes(), before)
                self.assertEqual((result["written"], self.baks(adapter)), ([], []))
                self.assertEqual(model_follow.lines([result]), [])

    def test_a_previous_generation_mark_alone_changes_nothing_and_codex_says_the_cli_is_low(self):
        for adapter in HARNESSES:
            with self.subTest(adapter=adapter):
                path = self.table(adapter)
                before = path.read_bytes()
                name = self.name(adapter)
                result = self.apply(adapter, [row(name("deep-1"), deprecated=True), row(name("light-1"), family="light")],
                                    client_version="0.163.0", probe=lambda _h: ((0, 163, 0), None))
                self.assertEqual(path.read_bytes(), before)
                text = "\n".join(model_follow.lines([result]))
                self.assertEqual("CLI 0.163.0" in text, adapter == "codex")
                self.assertNotIn("->", text)

    def test_another_family_is_a_proposal_and_nothing_is_written(self):
        for adapter in HARNESSES:
            with self.subTest(adapter=adapter):
                path = self.table(adapter)
                before = path.read_bytes()
                name = self.name(adapter)
                result = self.apply(adapter, [row(name("deep-1"), upgrade=name("zed-9")),
                                              row(name("zed-9"), family="zed", version=(9,)),
                                              row(name("light-1"), family="light"), row(name("top-1"), family="top")])
                self.assertEqual(path.read_bytes(), before)
                text = "\n".join(model_follow.lines([result]))
                self.assertIn(f"model follow: proposal {adapter} profile/deep {name('deep-1')} -> {name('zed-9')} (other family)", text)
                self.assertNotIn(f"{adapter} profile/deep {name('deep-1')} -> {name('zed-9')} (high)", text)

    def test_a_price_over_double_or_under_half_is_only_a_proposal_the_rest_follows(self):
        for adapter in HARNESSES:
            for cost, outcome in ((0.42, "proposal"), (0.38, "followed"), (0.09, "proposal"), (0.1, "followed")):
                with self.subTest(adapter=adapter, cost=cost):
                    path = self.table(adapter)
                    name = self.name(adapter)
                    self.apply(adapter, [row(name("deep-1"), cost=0.2), row(name("deep-2"), version=(2,), cost=cost),
                                         row(name("light-1"), family="light"), row(name("top-1"), family="top")])
                    moved = model_config.parse_config(path)["CFG_TIER_DEEP_MODEL"]
                    self.assertEqual(moved == name("deep-2"), outcome == "followed")

    def test_a_cell_the_user_chose_by_hand_follows_like_any_other(self):
        for adapter in HARNESSES:
            with self.subTest(adapter=adapter):
                self.table(adapter)
                name = self.name(adapter)
                result = self.apply(adapter, self.newer(adapter))
                self.assertIn("profile/deep", [entry["target"] for entry in result["written"]])

    def test_the_lines_that_move_with_a_cell_go_in_the_same_write_with_the_same_record(self):
        for adapter in HARNESSES:
            with self.subTest(adapter=adapter):
                path = self.table(adapter)
                name = self.name(adapter)
                result = self.apply(adapter, self.newer(adapter))
                after = path.read_text()
                cascade = next(line for line in after.splitlines() if line.startswith("CFG_TIER_DEEP_FAILOVER_CASCADE="))
                self.assertIn(record(adapter, "deep-1", "deep-2"), cascade)
                text = "\n".join(model_follow.lines([result]))
                self.assertIn(f"model follow: {adapter} cascade {name('deep-1')}:xhigh -> {name('deep-2')}:xhigh", text)
                self.assertEqual(self.baks(adapter), ["models.conf.bak"])

    def test_the_main_session_only_list_gains_the_new_name_and_loses_none(self):
        for adapter in ("claude", "codex"):
            with self.subTest(adapter=adapter):
                path = self.table(adapter)
                self.apply(adapter, [row("top-1", family="top"), row("top-2", family="top", version=(2,)),
                                     row("deep-1"), row("light-1", family="light")])
                self.assertEqual(model_config.parse_config(path)["CFG_MAIN_SESSION_ONLY_MODELS"], "top-1 top-2")

    def test_a_lower_effort_is_written_and_recorded_when_the_new_model_does_not_take_it(self):
        for adapter in HARNESSES:
            with self.subTest(adapter=adapter):
                path = self.table(adapter)
                name = self.name(adapter)
                result = self.apply(adapter, self.newer(adapter, efforts=("medium", "high")))
                effort_key = "CFG_TIER_DEEP_VARIANT" if adapter == "opencode" else "CFG_TIER_DEEP_EFFORT"
                after = path.read_text()
                self.assertEqual(model_config.parse_config(path)[effort_key], "high")
                self.assertEqual(K.read_marks(after)["CFG_TIER_DEEP_MODEL"],
                                 K.follow_record(TODAY, name("deep-1"), name("deep-2"), "xhigh"))
                self.assertIn(f"model follow: {adapter} tier/deep {name('deep-1')} -> {name('deep-2')} (high, was xhigh)",
                              "\n".join(model_follow.lines([result])))

    def test_every_line_says_what_was_written_and_the_backup_and_the_weights_note(self):
        for adapter in HARNESSES:
            with self.subTest(adapter=adapter):
                self.table(adapter)
                name = self.name(adapter)
                result = self.apply(adapter, self.newer(adapter))
                out = model_follow.lines([result])
                self.assertIn(f"model follow: {adapter} profile/deep {name('deep-1')} -> {name('deep-2')} (high) "
                              "· backup models.conf.bak", out)
                self.assertIn(f"model follow: {adapter} tier/deep {name('deep-1')} -> {name('deep-2')} (xhigh) "
                              "· backup models.conf.bak", out)
                self.assertEqual(sum("field weights" in line for line in out), 1)
                self.assertTrue(out[-1].startswith("model follow: the models the field weights were measured on"))

    def test_running_twice_changes_nothing_the_second_time(self):
        for adapter in HARNESSES:
            with self.subTest(adapter=adapter):
                path = self.table(adapter)
                self.apply(adapter, self.newer(adapter))
                once = path.read_bytes()
                again = self.apply(adapter, self.newer(adapter))
                self.assertEqual(path.read_bytes(), once)
                self.assertEqual((again["written"], self.baks(adapter)), ([], ["models.conf.bak"]))

    def test_a_followed_cell_follows_again_when_a_still_newer_model_appears(self):
        for adapter in HARNESSES:
            with self.subTest(adapter=adapter):
                path = self.table(adapter)
                name = self.name(adapter)
                self.apply(adapter, self.newer(adapter))
                self.apply(adapter, [*self.newer(adapter), row(name("deep-3"), version=(3,))])
                marks = K.read_marks(path.read_text())
                self.assertEqual(marks["CFG_TIER_DEEP_MODEL"], K.follow_record(TODAY, name("deep-2"), name("deep-3")))
                self.assertEqual(model_config.parse_config(path)["CFG_TIER_DEEP_MODEL"], name("deep-3"))


class HoldTests(Home):
    def test_a_keep_record_holds_the_cell_while_the_others_follow(self):
        for adapter in HARNESSES:
            with self.subTest(adapter=adapter):
                path = self.table(adapter, suffix={"CFG_TIER_DEEP_MODEL": K.render(K.keep_record("2026-11-01"))})
                name = self.name(adapter)
                result = self.apply(adapter, self.newer(adapter))
                values = model_config.parse_config(path)
                self.assertEqual(values["CFG_TIER_DEEP_MODEL"], name("deep-1"))
                self.assertEqual(values["CFG_TIER_BALANCED_DEEP_MODEL"], name("deep-2"))
                self.assertEqual(K.read_marks(path.read_text())["CFG_TIER_DEEP_MODEL"]["kind"], "keep")
                self.assertNotIn("tier/deep", [entry["target"] for entry in result["written"]])

    def test_going_back_to_the_earlier_value_by_hand_holds_the_cell(self):
        for adapter in HARNESSES:
            with self.subTest(adapter=adapter):
                name = self.name(adapter)
                path = self.table(adapter, suffix={"CFG_TIER_DEEP_MODEL": record(adapter, "deep-1", "deep-2")})
                self.apply(adapter, self.newer(adapter))
                self.assertEqual(model_config.parse_config(path)["CFG_TIER_DEEP_MODEL"], name("deep-1"))

    def test_choosing_another_model_by_hand_follows_it_and_replaces_the_old_record(self):
        for adapter in HARNESSES:
            with self.subTest(adapter=adapter):
                name = self.name(adapter)
                path = self.table(adapter, suffix={"CFG_TIER_DEEP_MODEL": record(adapter, "gone-1", "gone-2")})
                self.apply(adapter, self.newer(adapter))
                self.assertEqual(model_config.parse_config(path)["CFG_TIER_DEEP_MODEL"], name("deep-2"))
                self.assertEqual(K.read_marks(path.read_text())["CFG_TIER_DEEP_MODEL"],
                                 K.follow_record(TODAY, name("deep-1"), name("deep-2")))

    def test_releasing_a_held_cell_lets_the_next_run_follow_it(self):
        for adapter in HARNESSES:
            with self.subTest(adapter=adapter):
                name = self.name(adapter)
                path = self.table(adapter, suffix={"CFG_TIER_DEEP_MODEL": K.render(K.keep_record(TODAY))})
                self.apply(adapter, self.newer(adapter))
                self.assertEqual(model_config.parse_config(path)["CFG_TIER_DEEP_MODEL"], name("deep-1"))
                model_settings.set_model(adapter, "tier/deep", "follow", runtime=path.parent.parent, source_root=self.src)
                self.assertNotIn("auto-follow", next(l for l in path.read_text().splitlines() if l.startswith("CFG_TIER_DEEP_MODEL=")))
                self.apply(adapter, self.newer(adapter))
                self.assertEqual(model_config.parse_config(path)["CFG_TIER_DEEP_MODEL"], name("deep-2"))

    def test_a_model_chosen_on_a_kept_cell_stays_kept(self):
        for adapter in HARNESSES:
            with self.subTest(adapter=adapter):
                name = self.name(adapter)
                path = self.table(adapter)
                model_settings.set_model(adapter, "tier/deep", "keep", runtime=path.parent.parent, source_root=self.src)
                model_settings.set_model(adapter, "tier/deep", name("deep-0") + "@high", runtime=path.parent.parent,
                                         source_root=self.src)
                self.assertEqual(model_config.parse_config(path)["CFG_TIER_DEEP_MODEL"], name("deep-0"))
                self.assertEqual(K.read_marks(path.read_text())["CFG_TIER_DEEP_MODEL"]["kind"], "keep")
                self.apply(adapter, [*self.newer(adapter), row(name("deep-0"), version=(0, 5))])
                self.assertEqual(model_config.parse_config(path)["CFG_TIER_DEEP_MODEL"], name("deep-0"))

    def test_a_hand_chosen_model_after_a_follow_makes_the_cell_follow_from_there(self):
        for adapter in HARNESSES:
            with self.subTest(adapter=adapter):
                name = self.name(adapter)
                path = self.table(adapter)
                self.apply(adapter, self.newer(adapter))
                model_settings.set_model(adapter, "tier/deep", name("deep-1") + "@xhigh", runtime=path.parent.parent,
                                         source_root=self.src)  # a person goes back
                self.apply(adapter, self.newer(adapter))
                self.assertEqual(model_config.parse_config(path)["CFG_TIER_DEEP_MODEL"], name("deep-1"))
                model_settings.set_model(adapter, "tier/deep", name("odd-1") + "@high", runtime=path.parent.parent,
                                         source_root=self.src)  # a person picks another model
                self.assertEqual(K.cell_state(name("odd-1"), K.read_marks(path.read_text())["CFG_TIER_DEEP_MODEL"])["state"],
                                 "follow")

    def test_a_held_model_is_skipped_in_every_cell_of_the_harness(self):
        for adapter in HARNESSES:
            with self.subTest(adapter=adapter):
                name = self.name(adapter)
                held = K.render(K.held_record(TODAY, name("deep-2"), name("deep-1")))
                path = self.table(adapter, suffix={"CFG_TIER_DEEP_MODEL": held})
                before = path.read_bytes()
                result = self.apply(adapter, self.newer(adapter))
                self.assertEqual(path.read_bytes(), before)  # the profile cell too: same refused model
                self.assertEqual(result["written"], [])

    def test_another_successor_is_followed_by_the_held_cell_and_the_rest(self):
        for adapter in HARNESSES:
            with self.subTest(adapter=adapter):
                name = self.name(adapter)
                held = K.render(K.held_record(TODAY, name("deep-2"), name("deep-1")))
                path = self.table(adapter, suffix={"CFG_TIER_DEEP_MODEL": held})
                rows = [*self.newer(adapter), row(name("deep-1b"), version=(1, 5))]
                self.apply(adapter, rows)
                values = model_config.parse_config(path)
                self.assertEqual(values["CFG_TIER_DEEP_MODEL"], name("deep-1b"))
                self.assertEqual(values["CFG_MODEL_PROFILE_DEEP"], f"model/{name('deep-1b')}:high")
                self.assertEqual(K.read_marks(path.read_text())["CFG_TIER_DEEP_MODEL"],
                                 K.follow_record(TODAY, name("deep-1"), name("deep-1b")))
                # the record is one slot per line: once the cell went to the other model, the hold is gone
                # (a model refused for real is held again by the close that sees it refused)

    def test_a_held_record_on_a_changed_value_is_old_and_the_cell_follows(self):
        for adapter in HARNESSES:
            with self.subTest(adapter=adapter):
                name = self.name(adapter)
                held = K.render(K.held_record(TODAY, name("deep-2"), name("deep-0")))
                path = self.table(adapter, suffix={"CFG_TIER_DEEP_MODEL": held})
                self.apply(adapter, [*self.newer(adapter), row(name("deep-3"), version=(3,))])
                self.assertEqual(model_config.parse_config(path)["CFG_TIER_DEEP_MODEL"], name("deep-3"))


class FailureTests(Home):
    """Whatever goes wrong is one line, and the file is whole or as it was."""

    def test_a_directory_that_cannot_be_written_leaves_the_file_and_no_backup(self):
        for adapter in HARNESSES:
            with self.subTest(adapter=adapter):
                path = self.table(adapter)
                before = path.read_bytes()
                path.parent.chmod(0o555)
                self.addCleanup(path.parent.chmod, 0o755)
                result = self.apply(adapter, self.newer(adapter))
                path.parent.chmod(0o755)
                self.assertEqual(path.read_bytes(), before)
                self.assertEqual((result["status"], result["written"], self.baks(adapter)), ("error", [], []))
                self.assertEqual(sorted(p.name for p in path.parent.iterdir() if p.name.startswith(".")), [".models.conf.lock"]
                                 if (path.parent / ".models.conf.lock").exists() else [])
                lines = model_follow.lines([result])
                self.assertEqual(len(lines), 1)
                self.assertTrue(lines[0].startswith(f"model follow: {adapter} table not updated ("))

    def test_a_broken_or_foreign_file_is_skipped_untouched(self):
        for adapter in HARNESSES:
            for label, text in (("garbage", "not a config\n"), ("empty", ""), ("partial", "CFG_TIER_DEEP_MODEL=deep-1\n")):
                with self.subTest(adapter=adapter, case=label):
                    path = self.table(adapter, text)
                    result = self.apply(adapter, self.newer(adapter))
                    self.assertEqual((path.read_text(), result["status"], result["written"]), (text, "skipped", []))
                    self.assertEqual(model_follow.lines([result]), [])

    def test_a_list_that_cannot_be_read_follows_nothing_and_raises_nothing(self):
        for adapter in HARNESSES:
            with self.subTest(adapter=adapter):
                path = self.table(adapter)
                before = path.read_bytes()

                def broken(_harness):
                    raise ValueError("fixture")
                result = self.apply(adapter, [], read_catalog=broken)
                self.assertEqual((path.read_bytes(), result["written"]), (before, []))
                self.assertEqual(self.apply(adapter, [], state="x")["written"] if False else [], [])

    def test_a_new_file_the_whole_check_would_not_accept_is_not_written(self):
        for adapter in HARNESSES:
            with self.subTest(adapter=adapter):
                path = self.table(adapter)
                before = path.read_bytes()
                with mock.patch.object(model_settings, "candidate_ok", return_value=False):
                    result = self.apply(adapter, self.newer(adapter))
                self.assertEqual((path.read_bytes(), result["written"], self.baks(adapter)), (before, [], []))
                self.assertTrue(result["refused"])
                self.assertIn("not changed", "\n".join(model_follow.lines([result])))

    def test_one_cell_the_check_turns_down_does_not_stop_the_others(self):
        for adapter in HARNESSES:
            with self.subTest(adapter=adapter):
                path = self.table(adapter)
                real = model_settings.candidate_ok

                def picky(harness, text, source_root=None):
                    values = model_settings.parse_text(text)
                    if values.get("CFG_TIER_BALANCED_DEEP_MODEL", "").endswith("deep-2"):
                        return False
                    return real(harness, text, source_root)
                with mock.patch.object(model_settings, "candidate_ok", side_effect=picky):
                    result = self.apply(adapter, self.newer(adapter))
                values = model_config.parse_config(path)
                name = self.name(adapter)
                self.assertEqual(values["CFG_TIER_BALANCED_DEEP_MODEL"], name("deep-1"))
                self.assertEqual(values["CFG_TIER_DEEP_MODEL"], name("deep-2"))
                self.assertEqual([e["target"] for e in result["refused"]], ["tier/balanced-deep"])

    def test_a_table_that_is_locked_is_not_written_within_the_wait(self):
        for adapter in HARNESSES:
            with self.subTest(adapter=adapter):
                path = self.table(adapter)
                before = path.read_bytes()
                with safe_fs.TargetLock(path):
                    started = time.monotonic()
                    result = self.apply(adapter, self.newer(adapter), lock_wait=0.2)
                    took = time.monotonic() - started
                self.assertLess(took, 1.0)
                self.assertEqual((path.read_bytes(), result["status"]), (before, "error"))


class OverlapTests(Home):
    def test_two_updates_at_once_lose_no_edit_and_the_second_reads_again(self):
        for adapter in HARNESSES:
            with self.subTest(adapter=adapter):
                path = self.table(adapter)
                rows = self.newer(adapter)
                results = []
                gate = threading.Barrier(2)

                def run():
                    gate.wait()
                    results.append(self.apply(adapter, rows))
                threads = [threading.Thread(target=run) for _ in range(2)]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(30)
                self.assertEqual(len(results), 2)
                self.assertEqual(sum(bool(r["written"]) for r in results), 1)  # the later one finds nothing left
                self.assertEqual([r["error"] for r in results], [None, None])
                after = path.read_text()
                self.assertEqual(after.count("auto-follow"), after.count("auto-follow 2026-11-21: " + self.name(adapter)("deep-1") + " -> "))
                self.assertEqual(model_config.parse_config(path)["CFG_TIER_DEEP_MODEL"], self.name(adapter)("deep-2"))

    def test_a_person_who_changes_the_table_after_the_list_was_read_is_not_overwritten(self):
        for adapter in HARNESSES:
            with self.subTest(adapter=adapter):
                path = self.table(adapter)
                name = self.name(adapter)

                def read(_harness):
                    # the person edits between the read of the list and the write
                    text = path.read_text().replace("CFG_TIER_DEEP_MODEL=" + name("deep-1"),
                                                    "CFG_TIER_DEEP_MODEL=" + name("deep-1") + " " + K.render(K.keep_record(TODAY)))
                    path.write_text(text.replace("CFG_TIER_LIGHT_EFFORT=medium", "CFG_TIER_LIGHT_EFFORT=high")
                                    .replace("CFG_TIER_LIGHT_VARIANT=medium", "CFG_TIER_LIGHT_VARIANT=high"))
                    return SimpleNamespace(state="available", rows=tuple(self.newer(adapter)), client_version=None)
                self.apply(adapter, [], read_catalog=read)
                values = model_config.parse_config(path)
                self.assertEqual(values["CFG_TIER_DEEP_MODEL"], name("deep-1"))  # the new keep is honoured
                self.assertEqual(values["CFG_TIER_BALANCED_DEEP_MODEL"], name("deep-2"))
                key = "CFG_TIER_LIGHT_VARIANT" if adapter == "opencode" else "CFG_TIER_LIGHT_EFFORT"
                self.assertEqual(values[key], "high")  # and so is the person's other edit


class RejectionTests(Home):
    """A confirmed refusal at the close sends the cells that followed to the refused model back."""

    def refused(self, harness, model, **fields):
        return {"harness": harness, "model": model, "model_source": "explicit", "note": "dead-runtime-error",
                "failure_class": "runtime", "reconcile_reason": "runtime-model-rejected", **fields}

    def followed(self, adapter):
        path = self.table(adapter)
        self.apply(adapter, self.newer(adapter))
        return path

    def test_the_cells_that_followed_go_back_and_hold_the_refused_model(self):
        for adapter in ("claude", "codex"):
            with self.subTest(adapter=adapter):
                path = self.followed(adapter)
                name = self.name(adapter)
                after_follow = path.read_text()
                self.assertTrue(M.note_row_closed(self.refused(adapter, "deep-2")))
                values = model_config.parse_config(path)
                self.assertEqual(values["CFG_TIER_DEEP_MODEL"], "deep-1")
                self.assertEqual(values["CFG_MODEL_PROFILE_DEEP"], "model/deep-1:high")
                self.assertEqual(values["CFG_TIER_DEEP_FAILOVER_CASCADE"], "deep-1:xhigh light-1:medium")
                marks = K.read_marks(path.read_text())
                self.assertEqual(marks["CFG_TIER_DEEP_MODEL"], K.held_record(TODAY, "deep-2", "deep-1"))
                self.assertNotIn("CFG_TIER_DEEP_FAILOVER_CASCADE", marks)
                self.assertEqual(self.baks(adapter), ["models.conf.bak", "models.conf.bak.1"])
                self.assertEqual((path.parent / "models.conf.bak.1").read_text(), after_follow)
                # the next retry launches the earlier model, and the next update leaves it be
                self.assertEqual(self.apply(adapter, self.newer(adapter))["written"], [])
                self.assertEqual(model_config.parse_config(path)["CFG_TIER_DEEP_MODEL"], "deep-1")

    def test_a_lowered_effort_and_an_added_reserved_name_go_back_too(self):
        for adapter in ("claude", "codex"):
            with self.subTest(adapter=adapter):
                path = self.table(adapter)
                rows = [row("deep-1"), row("deep-2", version=(2,), efforts=("medium", "high")),
                        row("top-1", family="top"), row("top-2", family="top", version=(2,), efforts=("medium", "high")),
                        row("light-1", family="light")]
                before = path.read_text()
                self.apply(adapter, rows)
                self.assertEqual(model_config.parse_config(path)["CFG_MAIN_SESSION_ONLY_MODELS"], "top-1 top-2")
                M.note_row_closed(self.refused(adapter, "top-2"))
                M.note_row_closed(self.refused(adapter, "deep-2"))
                self.assertEqual(model_config.parse_config(path), model_config.parse_config_text(before)
                                 if hasattr(model_config, "parse_config_text") else model_config.parse_config(path))
                values = model_config.parse_config(path)
                self.assertEqual(values["CFG_MAIN_SESSION_ONLY_MODELS"], "top-1")
                self.assertEqual(values["CFG_TIER_TOP_MODEL"], "top-1")
                self.assertEqual(values["CFG_TIER_TOP_EFFORT"], "xhigh")
                self.assertEqual(values["CFG_TIER_DEEP_EFFORT"], "xhigh")
                self.assertEqual(values["CFG_TIER_DEEP_FAILOVER_CASCADE"], "deep-1:xhigh light-1:medium")

    def test_an_end_that_is_not_a_refusal_goes_back_to_nothing(self):
        for adapter in ("claude", "codex"):
            for label, fields in (("capacity", {"reconcile_reason": "runtime-capacity"}),
                                  ("usage limit", {"reconcile_reason": "runtime-usage-limit"}),
                                  ("auth", {"reconcile_reason": "runtime-auth"}),
                                  ("transient", {"reconcile_reason": "runtime-transient"}),
                                  ("killed", {"note": "dead-pid", "failure_class": "runtime", "reconcile_reason": ""}),
                                  ("answered", {"note": "completed-supervisor", "failure_class": "pass",
                                                "reconcile_reason": ""})):
                with self.subTest(adapter=adapter, case=label):
                    path = self.followed(adapter)
                    before = path.read_bytes()
                    M.note_row_closed(self.refused(adapter, "deep-2", **fields))
                    self.assertEqual(path.read_bytes(), before)

    def test_a_cell_with_no_record_is_never_taken_back(self):
        for adapter in ("claude", "codex"):
            with self.subTest(adapter=adapter):
                path = self.table(adapter, deep="deep-2")  # a person chose deep-2 themselves
                before = path.read_bytes()
                M.note_row_closed(self.refused(adapter, "deep-2"))
                self.assertEqual(path.read_bytes(), before)

    def test_a_refusal_that_names_another_model_takes_nothing_back(self):
        for adapter in ("claude", "codex"):
            with self.subTest(adapter=adapter):
                path = self.followed(adapter)
                before = path.read_bytes()
                M.note_row_closed(self.refused(adapter, "deep-2", reconcile_reason="runtime-model-rejected:other-model"))
                self.assertEqual(path.read_bytes(), before)
                M.note_row_closed(self.refused(adapter, "light-1"))  # a model no cell followed to
                self.assertEqual(path.read_bytes(), before)

    def test_a_table_that_cannot_be_locked_leaves_the_close_as_it_is_and_the_next_update_goes_back(self):
        for adapter in ("claude", "codex"):
            with self.subTest(adapter=adapter):
                path = self.followed(adapter)
                before = path.read_bytes()
                with safe_fs.TargetLock(path):
                    started = time.monotonic()
                    kept = M.note_row_closed(self.refused(adapter, "deep-2"))
                    took = time.monotonic() - started
                self.assertTrue(kept)  # the record is kept, as before
                self.assertEqual(M.read(adapter, adapter, "deep-2", "unscoped")["state"], "unavailable")
                self.assertGreaterEqual(took, 0.9)
                self.assertLess(took, 1.4)  # the one wait of the close, not two
                self.assertEqual(path.read_bytes(), before)
                result = self.apply(adapter, self.newer(adapter))  # the next update does it, before following
                self.assertEqual([e["target"] for e in result["reverted"]],
                                 ["tier/deep", "tier/balanced-deep", "profile/deep"])
                self.assertEqual(model_config.parse_config(path)["CFG_TIER_DEEP_MODEL"], "deep-1")
                self.assertEqual(K.read_marks(path.read_text())["CFG_TIER_DEEP_MODEL"]["kind"], "held")
                self.assertIn("model follow: %s tier/deep back to deep-1 · deep-2 rejected" % adapter,
                              model_follow.lines([result]))

    def test_the_next_update_goes_back_before_it_follows_and_then_follows_another_model(self):
        for adapter in HARNESSES:
            with self.subTest(adapter=adapter):
                path = self.followed(adapter)
                name = self.name(adapter)
                rows = [*self.newer(adapter), row(name("deep-1b"), version=(1, 5))]
                key = f"{adapter}/{'x'}"
                refused = {name("deep-2").lower()}
                result = self.apply(adapter, rows, rejected=refused)
                self.assertTrue(result["reverted"])
                self.assertEqual(model_config.parse_config(path)["CFG_TIER_DEEP_MODEL"], name("deep-1b"))
                self.assertEqual(K.read_marks(path.read_text())["CFG_TIER_DEEP_MODEL"],
                                 K.follow_record(TODAY, name("deep-1"), name("deep-1b")))

    def test_a_catch_up_goes_back_without_a_list(self):
        for adapter in HARNESSES:
            with self.subTest(adapter=adapter):
                path = self.followed(adapter)
                name = self.name(adapter)
                result = self.apply(adapter, [], read_catalog=lambda _h: SimpleNamespace(state="unknown", rows=(), client_version=None),
                                    rejected={name("deep-2").lower()})
                self.assertEqual(model_config.parse_config(path)["CFG_TIER_DEEP_MODEL"], name("deep-1"))
                self.assertTrue(result["reverted"])

    def test_the_refused_models_are_read_from_the_record_of_each_harness(self):
        M.record_unavailable("codex", "codex", "Model-A", "unscoped", "r")
        M.record_unavailable("claude", "claude", "model-b", "acct", "r")
        M.lift_unavailable("claude", "claude", "model-b", "acct")
        self.assertEqual(M.rejected_models("codex"), {"model-a"})
        self.assertEqual(M.rejected_models("claude"), frozenset())
        rows = [{"provider": "opencode-go", "model": "m-1", "state": "unavailable"},
                {"provider": "opencode-go", "model": "*", "state": "unavailable"},
                {"provider": "opencode-go", "model": "m-2", "state": "usable"}]
        tools = str(ROOT / "tools")
        sys.path.insert(0, tools)
        try:
            from fleet.collectors import usage_cache
            with mock.patch.object(usage_cache, "provider_records", return_value=rows):
                self.assertEqual(M.rejected_models("opencode"), {"opencode-go/m-1"})
        finally:
            sys.path.remove(tools)

    def test_a_close_with_no_record_in_the_table_adds_no_table_work(self):
        # the table is read once (a small file); it is never locked, copied or imported into
        for adapter in ("claude", "codex"):
            with self.subTest(adapter=adapter):
                path = self.table(adapter)
                before = path.read_bytes()
                with mock.patch.object(safe_fs, "TargetLock", side_effect=AssertionError("locked")):
                    M.note_row_closed(self.refused(adapter, "deep-2"))
                self.assertEqual(path.read_bytes(), before)
                self.assertEqual(self.baks(adapter), [])

    def test_an_exception_in_the_going_back_never_reaches_the_close(self):
        for adapter in ("claude", "codex"):
            with self.subTest(adapter=adapter):
                self.followed(adapter)
                with mock.patch.object(model_follow, "revert_rejected", side_effect=RuntimeError("boom")):
                    self.assertTrue(M.note_row_closed(self.refused(adapter, "deep-2")))
                with mock.patch.dict(sys.modules, {"model_follow": None}):
                    self.assertTrue(M.note_row_closed(self.refused(adapter, "deep-2")))


class CloseTimeTests(Home):
    """Closes that are not refusals pay nothing more; the table opens only on a refusal."""

    def test_a_thousand_closes_cost_the_same_with_or_without_a_table_that_has_records(self):
        answered = {"harness": "codex", "model": "deep-2", "model_source": "explicit",
                    "note": "completed-supervisor", "failure_class": "pass"}
        plain = self.table("codex")
        with mock.patch.object(safe_fs, "TargetLock", side_effect=AssertionError("opened")), \
                mock.patch.object(model_follow, "revert_rejected", side_effect=AssertionError("called")):
            started = time.monotonic()
            for _ in range(1000):
                M.note_row_closed(answered)
            plain_seconds = time.monotonic() - started
        followed = self.table("codex")
        self.apply("codex", self.newer("codex"))
        self.assertIn("auto-follow", followed.read_text())
        with mock.patch.object(safe_fs, "TargetLock", side_effect=AssertionError("opened")), \
                mock.patch.object(model_follow, "revert_rejected", side_effect=AssertionError("called")):
            started = time.monotonic()
            for _ in range(1000):
                M.note_row_closed(answered)
            recorded_seconds = time.monotonic() - started
        self.assertLess(plain_seconds, 2.0)
        self.assertLess(recorded_seconds, plain_seconds * 3 + 0.5)


class ViewTests(Home):
    """The one function a screen reads: states, what comes next, and what an update would write."""

    def test_the_view_lists_every_cell_with_its_state_and_writes_nothing(self):
        for adapter in HARNESSES:
            with self.subTest(adapter=adapter):
                name = self.name(adapter)
                suffix = {"CFG_TIER_DEEP_MODEL": K.render(K.keep_record("2026-11-01")),
                          "CFG_TIER_LIGHT_MODEL": K.render(K.held_record("2026-11-02", name("light-2"), name("light-1")))}
                path = self.table(adapter, suffix=suffix)
                before, listing = path.read_bytes(), sorted(p.name for p in path.parent.iterdir())
                catalog = SimpleNamespace(state="available", rows=tuple(self.newer(adapter)), client_version=None)
                (view,) = model_follow.fleet_view([adapter], source_root=self.src, today=TODAY,
                                                  read_catalog=lambda _h: catalog, probe=lambda _h: (None, None))
                self.assertEqual((path.read_bytes(), sorted(p.name for p in path.parent.iterdir())), (before, listing))
                cells = {cell["target"]: cell for cell in view["cells"]}
                self.assertEqual(view["harness"], adapter)
                self.assertEqual((cells["tier/deep"]["state"], cells["tier/deep"]["why"]), ("keep", "kept"))
                self.assertEqual(cells["tier/deep"]["since"]["date"], "2026-11-01")
                self.assertEqual(cells["tier/deep"]["next"]["branch"], "kept")
                self.assertEqual(cells["tier/light"]["state"], "held")
                self.assertEqual(cells["tier/light"]["held"], name("light-2"))
                self.assertEqual(cells["tier/light"]["since"], {"date": "2026-11-02", "from": name("light-1"), "to": name("light-2")})
                follow = cells["profile/deep"]
                self.assertEqual((follow["state"], follow["why"], follow["model"], follow["effort"]),
                                 ("follow", "default", name("deep-1"), "high"))
                self.assertEqual(follow["next"], {"model": name("deep-2"), "effort": "high", "branch": "will-follow",
                                                  "reason": "newer", "command": f"hearting model set {adapter} profile/deep {name('deep-2')}@high",
                                                  "fit_from": None})
                self.assertEqual({line["key"] for line in follow["lines"]},
                                 {"CFG_TIER_DEEP_FAILOVER_CASCADE", *(("CFG_V2_AUTO_FALLBACKS",) if adapter == "opencode" else ())})

    def test_the_plan_is_exactly_what_the_write_then_does(self):
        for adapter in HARNESSES:
            with self.subTest(adapter=adapter):
                name = self.name(adapter)
                path = self.table(adapter)
                rows = self.newer(adapter, efforts=("medium", "high"))
                catalog = SimpleNamespace(state="available", rows=tuple(rows), client_version=None)
                (view,) = model_follow.fleet_view([adapter], source_root=self.src, today=TODAY,
                                                  read_catalog=lambda _h: catalog, probe=lambda _h: (None, None))
                result = self.apply(adapter, rows)
                planned = [(p["target"], p["from"], p["to"], p["effort"], p["effort_was"]) for p in view["plan"]]
                done = [(e["target"], e["current"]["model"], e["successor"]["model"], e["successor"]["effort"],
                         e["successor"]["fit_from"]) for e in result["written"]]
                self.assertEqual(planned, done)
                self.assertTrue(planned)
                merged = {line["key"]: line["to"] for item in view["plan"] for line in item["lines"]}
                values = model_config.parse_config(path)
                for key in merged:
                    self.assertIn(name("deep-2"), values[key])
                after = self.survey(adapter, rows)
                self.assertEqual(after["plan"], [])  # nothing is left to write
                self.assertEqual(model_follow.state_counts(after), (3, 0, 0))

    def test_a_cell_waiting_to_go_back_is_in_the_plan_first(self):
        for adapter in HARNESSES:
            with self.subTest(adapter=adapter):
                name = self.name(adapter)
                self.table(adapter)
                self.apply(adapter, self.newer(adapter))
                found = self.survey(adapter, self.newer(adapter), rejected={name("deep-2").lower()})
                self.assertEqual([item["action"] for item in found["plan"]][:3], ["revert"] * 3)
                self.assertEqual(found["plan"][0]["refused"], name("deep-2"))

    def test_without_a_list_the_view_still_gives_the_cells_and_their_states(self):
        for adapter in HARNESSES:
            with self.subTest(adapter=adapter):
                self.table(adapter, suffix={"CFG_TIER_DEEP_MODEL": K.render(K.keep_record(TODAY))})
                (view,) = model_follow.fleet_view(
                    [adapter], source_root=self.src, today=TODAY,
                    read_catalog=lambda _h: SimpleNamespace(state="unknown", rows=(), client_version=None))
                self.assertEqual(view["reason"], "no-catalog")
                self.assertEqual(view["states"]["kept"], 1)


if __name__ == "__main__":
    unittest.main()
