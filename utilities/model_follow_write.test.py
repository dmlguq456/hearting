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
        day = mock.patch.object(K, "today", return_value=TODAY)  # the close has no `today` argument
        day.start()
        self.addCleanup(day.stop)
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

    def test_releasing_a_held_cell_does_not_follow_to_a_model_the_record_still_shows_refused(self):
        for adapter in HARNESSES:
            with self.subTest(adapter=adapter):
                name = self.name(adapter)
                refused = {name("deep-2").lower()}
                held = K.render(K.held_record(TODAY, name("deep-2"), name("deep-1")))
                path = self.table(adapter, suffix={"CFG_TIER_DEEP_MODEL": held})
                model_settings.set_model(adapter, "tier/deep", "follow", runtime=path.parent.parent, source_root=self.src)
                released, baks = path.read_bytes(), self.baks(adapter)
                for _ in range(3):  # the person's `follow` removed the record; the refusal is still on record
                    result = self.apply(adapter, self.newer(adapter), rejected=refused)
                    self.assertEqual((result["written"], result["reverted"]), ([], []))
                self.assertEqual((path.read_bytes(), self.baks(adapter)), (released, baks))
                # once nothing refuses it any more the cell follows, as before
                self.apply(adapter, self.newer(adapter), rejected=())
                self.assertEqual(model_config.parse_config(path)["CFG_TIER_DEEP_MODEL"], name("deep-2"))

    def test_a_model_a_person_chose_that_the_record_shows_refused_is_not_followed_to_by_another_cell(self):
        for adapter in HARNESSES:
            with self.subTest(adapter=adapter):
                name = self.name(adapter)
                refused = {name("deep-2").lower()}
                path = self.table(adapter)
                name_deep = f"CFG_TIER_DEEP_MODEL={name('deep-1')}"
                path.write_text(path.read_text().replace(name_deep, f"CFG_TIER_DEEP_MODEL={name('deep-2')}"))
                before = path.read_bytes()  # one cell chosen by hand at the refused model, with no record
                rows = self.newer(adapter)
                plan = self.survey(adapter, rows, rejected=refused)["plan"]
                result = self.apply(adapter, rows, rejected=refused)
                self.assertEqual(([p["target"] for p in plan], result["written"], result["reverted"]), ([], [], []))
                self.assertEqual((path.read_bytes(), self.baks(adapter)), (before, []))

    def test_a_held_record_on_another_value_does_not_make_the_next_update_follow_to_the_refused_model(self):
        for adapter in HARNESSES:
            with self.subTest(adapter=adapter):
                name = self.name(adapter)
                refused = {name("deep-2").lower()}
                held = K.render(K.held_record(TODAY, name("deep-2"), name("deep-1")))
                path = self.table(adapter, deep="deep-0", suffix={"CFG_TIER_DEEP_MODEL": held})
                rows = [row(name("deep-0"), version=(0, 5)), *self.newer(adapter)]
                first = self.apply(adapter, rows, rejected=refused)
                self.assertEqual(model_config.parse_config(path)["CFG_TIER_DEEP_MODEL"], name("deep-1"))
                self.assertTrue(first["written"])
                after_first, baks = path.read_bytes(), self.baks(adapter)
                for _ in range(2):  # no pass in this update goes on to deep-2 or back
                    again = self.apply(adapter, rows, rejected=refused)
                    self.assertEqual((again["written"], again["reverted"]), ([], []))
                self.assertEqual((path.read_bytes(), self.baks(adapter)), (after_first, baks))

    def test_the_view_says_which_refused_model_a_skipped_cell_would_have_followed_to(self):
        for adapter in HARNESSES:
            with self.subTest(adapter=adapter):
                name = self.name(adapter)
                self.table(adapter)
                catalog = SimpleNamespace(state="available", rows=tuple(self.newer(adapter)), client_version=None)
                (view,) = model_follow.fleet_view([adapter], source_root=self.src, today=TODAY,
                                                  read_catalog=lambda _h: catalog, probe=lambda _h: (None, None),
                                                  rejected={name("deep-2").lower()})
                cell = {c["target"]: c for c in view["cells"]}["tier/deep"]
                self.assertEqual((cell["held"], cell["next"], view["plan"]), (name("deep-2"), None, []))

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
        self.assertEqual(M.rejected_models("opencode"), frozenset())  # no usage cache at all

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
                self.assertEqual(cells["tier/light"]["since"], {"date": "2026-11-02", "from": name("light-1"), "to": name("light-2"),
                                                                "effort_was": None, "lines": []})
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

    def view(self, adapter, rows):
        catalog = SimpleNamespace(state="available", rows=tuple(rows), client_version=None)
        (view,) = model_follow.fleet_view([adapter], source_root=self.src, today=TODAY,
                                          read_catalog=lambda _h: catalog, probe=lambda _h: (None, None))
        return {cell["target"]: cell for cell in view["cells"]}

    def test_since_gives_the_effort_a_follow_lowered(self):
        for adapter in HARNESSES:
            with self.subTest(adapter=adapter):
                self.table(adapter)
                rows = self.newer(adapter, efforts=("medium", "high"))
                self.apply(adapter, rows)
                cells = self.view(adapter, rows)
                self.assertEqual(cells["tier/deep"]["effort"], "high")
                self.assertEqual(cells["tier/deep"]["since"]["effort_was"], "xhigh")
                self.assertIsNone(cells["profile/deep"]["since"]["effort_was"])  # high stayed high
                self.assertEqual(cells["tier/deep"]["since"]["from"], self.name(adapter)("deep-1"))

    def test_since_gives_the_lines_the_follow_moved_as_the_table_records_them(self):
        for adapter in HARNESSES:
            with self.subTest(adapter=adapter):
                path = self.table(adapter)
                rows = self.newer(adapter)
                self.apply(adapter, rows)
                wanted = {"CFG_TIER_DEEP_FAILOVER_CASCADE", *(("CFG_V2_AUTO_FALLBACKS",) if adapter == "opencode" else ())}
                cells = self.view(adapter, rows)
                self.assertEqual(set(cells["tier/deep"]["since"]["lines"]), wanted)
                self.assertEqual(cells["tier/light"]["since"], None)  # nothing newer: no follow, no lines
                # a line whose record the person removed is theirs: it is not counted
                text = "\n".join(l.split(" # auto-follow")[0] if l.startswith("CFG_TIER_DEEP_FAILOVER_CASCADE=") else l
                                 for l in path.read_text().splitlines()) + "\n"
                path.write_text(text)
                self.assertNotIn("CFG_TIER_DEEP_FAILOVER_CASCADE", self.view(adapter, rows)["tier/deep"]["since"]["lines"])

    def test_since_counts_the_main_session_only_name_a_follow_added(self):
        for adapter in ("claude", "codex"):
            with self.subTest(adapter=adapter):
                self.table(adapter)
                rows = [*self.newer(adapter), row("top-2", family="top", version=(2,))]
                self.apply(adapter, rows)
                self.assertIn("CFG_MAIN_SESSION_ONLY_MODELS", self.view(adapter, rows)["tier/top"]["since"]["lines"])

    def test_a_cell_no_update_can_follow_is_told_from_an_ordinary_one(self):
        for adapter in HARNESSES:
            with self.subTest(adapter=adapter):
                name = self.name(adapter)
                path = self.table(adapter)
                path.write_text(path.read_text().replace(f"CFG_TIER_MINI_MODEL={name('light-1')}", "CFG_TIER_MINI_MODEL=odd-alias"))
                cells = self.view(adapter, self.newer(adapter))
                self.assertEqual((cells["tier/mini"]["followable"], cells["tier/mini"]["reason"]),
                                 (False, "not-in-list"))
                self.assertEqual((cells["tier/light"]["followable"], cells["tier/light"]["next"]), (True, None))  # nothing newer
                self.assertEqual((cells["tier/deep"]["followable"], cells["tier/deep"]["next"]["branch"]),
                                 (True, "will-follow"))
                self.assertIs(cells["tier/mini"]["state"] == "follow" and cells["tier/mini"]["next"] is None, True)

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
                cells = {cell["target"]: cell for cell in view["cells"]}
                self.assertEqual(sorted(cells), sorted(c["target"] for c in model_follow.cells(
                    adapter, model_config.parse_config(self.path(adapter)))))
                deep = cells["tier/deep"]
                self.assertEqual((deep["model"], deep["effort"], deep["state"], deep["why"], deep["held"], deep["next"]),
                                 (self.name(adapter)("deep-1"), "xhigh", "keep", "kept", None, None))
                self.assertEqual(deep["since"], {"date": TODAY, "from": None, "to": None, "effort_was": None, "lines": []})
                self.assertEqual((deep["followable"], deep["reason"]), (None, "no-catalog"))
                self.assertEqual(cells["tier/light"]["state"], "follow")

    def test_a_held_cell_is_given_with_its_refusal_when_there_is_no_list(self):
        for adapter in HARNESSES:
            with self.subTest(adapter=adapter):
                name = self.name(adapter)
                self.table(adapter, suffix={"CFG_TIER_DEEP_MODEL": K.render(K.held_record(TODAY, name("deep-2"), name("deep-1")))})
                (view,) = model_follow.fleet_view(
                    [adapter], source_root=self.src, today=TODAY,
                    read_catalog=lambda _h: SimpleNamespace(state="unknown", rows=(), client_version=None))
                cell = {c["target"]: c for c in view["cells"]}["tier/deep"]
                self.assertEqual((cell["state"], cell["held"], cell["next"]), ("held", name("deep-2"), None))


class WhoseTableTests(Home):
    """A pinned config home belongs to one harness: a pass or a close for another harness
    never writes it."""

    def test_a_pass_and_a_close_for_another_harness_leave_a_pinned_foreign_table_alone(self):
        claude, codex = self.table("claude"), self.table("codex")
        before = (claude.read_bytes(), claude.stat().st_mtime_ns)
        with mock.patch.dict(os.environ, {"HEARTING_MODEL_CONFIG_HOME": str(claude.parent.parent)}):
            self.apply("codex", self.newer("codex"))
            self.assertEqual(model_config.parse_config(codex)["CFG_TIER_DEEP_MODEL"], "deep-2")  # its own table
            self.assertTrue(model_follow.revert_rejected("codex", "deep-2", source_root=self.src))  # codex's own
            self.apply("codex", self.newer("codex"), rejected={"deep-2"})
        self.assertEqual((claude.read_bytes(), claude.stat().st_mtime_ns), before)
        self.assertEqual(self.baks("claude"), [])


class ReadOnlyTableTests(Home):
    """A table the person made read-only is theirs: nothing writes it on its own."""

    def read_only(self, adapter, **kwargs):
        path = self.table(adapter, **kwargs)
        path.chmod(0o444)
        self.addCleanup(path.chmod, 0o644)
        return path

    def test_a_read_only_table_with_something_to_follow_is_left_as_it_is_and_says_so_in_one_line(self):
        for adapter in HARNESSES:
            with self.subTest(adapter=adapter):
                path = self.read_only(adapter)
                before = (path.read_bytes(), path.stat().st_mode, path.stat().st_mtime_ns)
                result = self.apply(adapter, self.newer(adapter))
                self.assertEqual((result["written"], result["reverted"], result["read_only"]), ([], [], True))
                self.assertEqual((path.read_bytes(), path.stat().st_mode, path.stat().st_mtime_ns), before)
                self.assertEqual(self.baks(adapter), [])
                self.assertEqual([t for t in model_follow.lines([result]) if "table" in t],
                                 [f"model follow: {adapter} table is read-only · not updated"])
                path.chmod(0o644)  # the person lets it be written: it follows at once
                self.apply(adapter, self.newer(adapter))
                self.assertEqual(model_config.parse_config(path)["CFG_TIER_DEEP_MODEL"], self.name(adapter)("deep-2"))

    def test_a_read_only_table_with_nothing_to_follow_says_nothing(self):
        for adapter in HARNESSES:
            with self.subTest(adapter=adapter):
                self.read_only(adapter)
                result = self.apply(adapter, [])
                self.assertEqual((result["read_only"], model_follow.lines([result])), (False, []))

    def test_the_plan_of_a_read_only_table_is_empty_so_plan_and_write_agree(self):
        for adapter in HARNESSES:
            with self.subTest(adapter=adapter):
                self.read_only(adapter)
                found = self.survey(adapter, self.newer(adapter))
                self.assertEqual((found["plan"], found["read_only"]), ([], True))
                catalog = SimpleNamespace(state="available", rows=tuple(self.newer(adapter)), client_version=None)
                (view,) = model_follow.fleet_view([adapter], source_root=self.src, today=TODAY,
                                                  read_catalog=lambda _h: catalog, probe=lambda _h: (None, None))
                self.assertEqual((view["plan"], view["read_only"]), ([], True))

    def test_a_refusal_at_the_close_does_not_write_a_read_only_table(self):
        for adapter in ("claude", "codex"):
            with self.subTest(adapter=adapter):
                path = self.table(adapter)
                self.apply(adapter, self.newer(adapter))
                path.chmod(0o444)
                self.addCleanup(path.chmod, 0o644)
                before = (path.read_bytes(), path.stat().st_mtime_ns)
                self.assertFalse(model_follow.revert_rejected(adapter, "deep-2", source_root=self.src))
                self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)
                result = self.apply(adapter, self.newer(adapter), rejected={"deep-2"})  # the next update says so
                self.assertEqual((result["read_only"], result["reverted"]), (True, []))

    def test_the_person_setting_a_model_by_hand_still_writes_a_read_only_table(self):
        for adapter in HARNESSES:
            with self.subTest(adapter=adapter):
                path = self.read_only(adapter)
                done = model_settings.set_model(adapter, "tier/deep", self.name(adapter)("deep-3") + "@high",
                                                runtime=path.parent.parent, source_root=self.src)
                self.assertEqual(done["status"], "changed")
                self.assertEqual(model_config.parse_config(path)["CFG_TIER_DEEP_MODEL"], self.name(adapter)("deep-3"))


class GoingBackTests(Home):
    """A cell goes back with only the lines its follow moved."""

    def test_a_line_the_person_set_by_hand_is_not_taken_back_with_the_cell(self):
        for adapter in ("claude", "codex"):
            with self.subTest(adapter=adapter):
                path = self.table(adapter)
                text = path.read_text()
                # the person set tier/deep and its cascade to deep-2 by hand: no record on either line
                text = text.replace("CFG_TIER_DEEP_MODEL=deep-1", "CFG_TIER_DEEP_MODEL=deep-2")
                text = text.replace('CFG_TIER_DEEP_FAILOVER_CASCADE="deep-1:xhigh', 'CFG_TIER_DEEP_FAILOVER_CASCADE="deep-2:xhigh')
                path.write_text(text)
                rows = [row("deep-1"), row("deep-2", version=(2,)), row("light-1", family="light"), row("top-1", family="top")]
                self.apply(adapter, rows)  # profile/deep follows deep-1 -> deep-2
                self.assertEqual(model_config.parse_config(path)["CFG_MODEL_PROFILE_DEEP"], "model/deep-2:high")
                self.assertTrue(M.note_row_closed(RejectionTests.refused(self, adapter, "deep-2")))
                values = model_config.parse_config(path)
                self.assertEqual(values["CFG_MODEL_PROFILE_DEEP"], "model/deep-1:high")
                self.assertEqual(values["CFG_TIER_DEEP_MODEL"], "deep-2")
                self.assertEqual(values["CFG_TIER_DEEP_FAILOVER_CASCADE"], "deep-2:xhigh light-1:medium")

    def test_a_name_the_person_put_in_the_main_session_only_list_stays_when_the_cell_goes_back(self):
        for adapter in ("claude", "codex"):
            with self.subTest(adapter=adapter):
                path = self.table(adapter)
                path.write_text(path.read_text().replace('CFG_MAIN_SESSION_ONLY_MODELS="top-1"',
                                                         'CFG_MAIN_SESSION_ONLY_MODELS="top-1 top-2"'))
                rows = [row("top-1", family="top"), row("top-2", family="top", version=(2,)),
                        row("deep-1"), row("light-1", family="light")]
                self.apply(adapter, rows)
                self.assertEqual(model_config.parse_config(path)["CFG_TIER_TOP_MODEL"], "top-2")
                M.note_row_closed(RejectionTests.refused(self, adapter, "top-2"))
                values = model_config.parse_config(path)
                self.assertEqual((values["CFG_TIER_TOP_MODEL"], values["CFG_MAIN_SESSION_ONLY_MODELS"]),
                                 ("top-1", "top-1 top-2"))

    def test_a_going_back_says_which_items_of_a_shared_line_moved(self):
        for adapter in HARNESSES:
            with self.subTest(adapter=adapter):
                path = self.table(adapter)
                name = self.name(adapter)
                self.apply(adapter, self.newer(adapter))
                self.apply(adapter, self.newer(adapter), rejected={name("deep-2").lower()})
                said = model_follow.lines([self.apply(adapter, self.newer(adapter), rejected={name("deep-2").lower()})])
                self.assertEqual(said, [])  # the same line is not said again
                if adapter == "opencode":
                    return
                path = self.table(adapter)
                self.apply(adapter, self.newer(adapter))
                result = self.apply(adapter, self.newer(adapter), rejected={name("deep-2").lower()})
                self.assertIn(f"model follow: {adapter} cascade {name('deep-2')}:xhigh -> {name('deep-1')}:xhigh",
                              model_follow.lines([result]))


class MarkEdgeTests(Home):
    def test_follow_on_a_line_with_no_record_writes_nothing(self):
        for adapter in HARNESSES:
            with self.subTest(adapter=adapter):
                path = self.table(adapter, suffix={"CFG_TIER_DEEP_MODEL": "#   my  note   "})
                before = path.read_bytes()
                done = model_settings.set_model(adapter, "tier/deep", "follow", runtime=path.parent.parent,
                                                source_root=self.src)
                self.assertEqual((done["status"], done["backup"], done["changed_keys"]), ("unchanged", None, []))
                self.assertEqual((path.read_bytes(), self.baks(adapter)), (before, []))

    def test_a_line_with_more_than_one_record_is_held_by_the_person_until_they_follow(self):
        for adapter in HARNESSES:
            with self.subTest(adapter=adapter):
                name = self.name(adapter)
                two = K.render(K.keep_record("2026-11-20")) + " " + record(adapter, "deep-1", "deep-2")
                path = self.table(adapter, suffix={"CFG_TIER_DEEP_MODEL": two})
                self.assertEqual(K.cell_state(name("deep-1"), K.read_marks(path.read_text())["CFG_TIER_DEEP_MODEL"])["state"], "keep")
                result = self.apply(adapter, self.newer(adapter))
                self.assertEqual(model_config.parse_config(path)["CFG_TIER_DEEP_MODEL"], name("deep-1"))
                self.assertNotIn("tier/deep", [e["target"] for e in result["written"]])
                model_settings.set_model(adapter, "tier/deep", "follow", runtime=path.parent.parent, source_root=self.src)
                self.apply(adapter, self.newer(adapter))
                self.assertEqual(model_config.parse_config(path)["CFG_TIER_DEEP_MODEL"], name("deep-2"))

    def test_a_proposal_gives_the_price_to_two_decimals(self):
        found = model_follow.successor(
            [row("a-1", cost=10.0), row("a-2", version=(2,), cost=4.5)], "a-1")
        self.assertEqual((found["kind"], found["reason"]), ("proposal", "price x0.45"))


class OpenCodeRejectionTests(Home):
    """OpenCode's refusals are the model-level lines of its usage cache for the account in use."""
    SCOPE, GENERATION = "acct-1", "gen-1"

    def setUp(self):
        super().setUp()
        for key, value in (("OPENCODE_RUNTIME_VERSION", "1.19.0"),
                           ("FLEET_USAGE_STATE_DIR", str(self.root / "usage"))):
            patcher = mock.patch.dict(os.environ, {key: value})
            patcher.start()
            self.addCleanup(patcher.stop)
        tools = str(ROOT / "tools")
        sys.path.insert(0, tools)
        self.addCleanup(sys.path.remove, tools)
        from fleet.collectors import usage_cache
        import opencode_runtime
        self.cache, self.runtime = usage_cache, opencode_runtime
        account = opencode_runtime.AccountScopeObservation("available", self.SCOPE, None, self.GENERATION)
        patcher = mock.patch.object(opencode_runtime, "provider_account_observation", return_value=account)
        patcher.start()
        self.addCleanup(patcher.stop)

    def put(self, provider, model, state, reason=None, *, scope=None, generation=None):
        self.assertTrue(self.cache.record_provider(
            "opencode", provider, model, scope or self.SCOPE, state, reason=reason, source="opencode-run",
            credential_generation=generation or self.GENERATION))

    def test_only_a_model_level_reason_counts_as_a_refusal(self):
        for reason, counted in (("model-not-found", True), ("model-unavailable", True),
                                ("credential-expired", False), ("authentication-rejected", False),
                                ("subscription-missing", False), ("something-else", False), (None, False)):
            with self.subTest(reason=reason):
                self.put("opencode-go", "m-1", "unavailable", reason)
                self.assertEqual(M.rejected_models("opencode"),
                                 {"opencode-go/m-1"} if counted else frozenset())
                self.put("opencode-go", "m-1", "usable")

    def test_a_refusal_is_seen_however_many_other_lines_follow_it_by_name(self):
        for index in range(40):
            self.put("zz-provider", f"m-{index:02d}", "usable")
        self.put("aa-provider", "refused-model", "unavailable", "model-not-found")
        self.assertEqual(M.rejected_models("opencode"), {"aa-provider/refused-model"})

    def test_a_line_of_another_account_or_another_credential_generation_is_not_known_to_refuse(self):
        self.put("opencode-go", "m-1", "unavailable", "model-not-found", scope="acct-2")
        self.put("opencode-go", "m-2", "unavailable", "model-not-found", generation="gen-old")
        self.assertEqual(M.rejected_models("opencode"), frozenset())
        self.put("opencode-go", "m-3", "unavailable", "model-not-found")
        self.assertEqual(M.rejected_models("opencode"), {"opencode-go/m-3"})

    def test_an_account_wide_line_is_not_a_model_refusal(self):
        self.put("opencode-go", "*", "unavailable", "model-not-found")
        self.assertEqual(M.rejected_models("opencode"), frozenset())

    def test_no_cache_and_a_broken_cache_give_nothing(self):
        self.assertEqual(M.rejected_models("opencode"), frozenset())
        path = Path(self.cache._path("opencode"))
        path.parent.mkdir(parents=True, exist_ok=True)
        for broken in ("{", "[]", '{"schema_version": 99}', '{"schema_version": 1, "harness": "opencode", "providers": []}'):
            path.write_text(broken)
            self.assertEqual(M.rejected_models("opencode"), frozenset())

    def test_an_expired_key_does_not_send_a_followed_cell_back_and_a_refused_model_does(self):
        provider = "prov"
        name = lambda model: f"{provider}/{model}"  # noqa: E731
        path = self.table("opencode", deep="deep-2", suffix={
            "CFG_TIER_DEEP_MODEL": K.render(K.follow_record(TODAY, name("deep-1"), name("deep-2")))})
        rows = [row(name("deep-1")), row(name("deep-2"), version=(2,))]
        self.put(provider, "deep-2", "unavailable", "credential-expired")
        before = path.read_bytes()
        result = self.apply("opencode", rows, rejected=None)
        self.assertEqual((result["reverted"], path.read_bytes()), ([], before))
        self.put(provider, "deep-2", "unavailable", "model-not-found")
        result = self.apply("opencode", rows, rejected=None)
        self.assertTrue(result["reverted"])
        self.assertEqual(K.read_marks(path.read_text())["CFG_TIER_DEEP_MODEL"]["kind"], "held")


FAKE_CODEX = """#!/bin/sh
d="$FAKE_DIR"
case "$1" in
  --version) echo "codex-cli 0.171.0" ;;
  debug)
    echo x >> "$d/count"
    case "$(cat "$d/mode")" in
      ok) printf '{"client_version":"0.171.0","newer":true}' > "$CODEX_HOME/models_cache.json" ;;
      fail) exit 3 ;;
      same) : ;;
      sleep) echo $$ > "$d/pid"; exec sleep 30 ;;
    esac ;;
esac
"""


class StaleListTests(Home):
    """A list another CLI version wrote is refreshed once by the installed CLI, else nothing follows."""

    def setUp(self):
        super().setUp()
        self.fake = self.root / "fake"
        (self.fake / "bin").mkdir(parents=True)
        cli = self.fake / "bin" / "codex"
        cli.write_text(FAKE_CODEX)
        cli.chmod(0o755)
        self.cli = str(cli)
        os.environ.update(FAKE_DIR=str(self.fake), PATH=f"{self.fake / 'bin'}:/usr/bin:/bin")
        self.addCleanup(self.reap)
        (self.root / "codex").mkdir(exist_ok=True)

    def reap(self):
        pid = self.fake / "pid"
        if pid.exists():
            try:
                os.killpg(int(pid.read_text()), 9)
            except (OSError, ValueError):
                pass

    def mode(self, mode):
        (self.fake / "mode").write_text(mode)

    def calls(self):
        count = self.fake / "count"
        return len(count.read_text().split()) if count.exists() else 0

    def cache(self, version, newer=False):
        document = {"newer": newer}
        if version is not None:
            document["client_version"] = version
        (self.root / "codex" / "models_cache.json").write_text(json.dumps(document))

    def read(self, _harness):
        document = json.loads((self.root / "codex" / "models_cache.json").read_text())
        rows = self.newer("codex") if document.get("newer") else [
            row("deep-1"), row("light-1", family="light"), row("top-1", family="top")]
        return SimpleNamespace(state="available", rows=tuple(rows),
                               client_version=document.get("client_version"))

    def go(self, **kwargs):
        kwargs.setdefault("read_catalog", self.read)
        kwargs.setdefault("probe", lambda _h: ((0, 171, 0), self.cli))
        return model_follow.apply_harness("codex", source_root=self.src, today=TODAY, **kwargs)

    def stat(self, path):
        info = path.stat()
        return path.read_bytes(), info.st_mtime_ns, info.st_size

    def tree(self):
        return {str(p): (p.stat().st_size, p.stat().st_mtime_ns) for p in self.root.rglob("*") if p.is_file()}

    LINE = ("model follow: codex: the model list was written by CLI 0.163.0, installed is 0.171.0 "
            "· start codex once to refresh it · nothing followed")

    def test_a_refresh_that_brings_the_installed_version_lets_the_pass_follow(self):
        path = self.table("codex")
        self.cache("0.163.0")
        self.mode("ok")
        result = self.go()
        self.assertEqual(self.calls(), 1)
        self.assertEqual((result["error"], result["cli"], result["reason"]), (None, None, ""))
        self.assertEqual(model_config.parse_config(path)["CFG_TIER_DEEP_MODEL"], "deep-2")
        self.assertFalse([t for t in model_follow.lines([result]) if "nothing followed" in t])
        again = self.go()  # the list is the installed CLI's now: no second refresh
        self.assertEqual((self.calls(), again["written"]), (1, []))

    def test_a_refresh_that_fails_times_out_or_changes_nothing_leaves_the_table_and_says_so(self):
        for case in ("fail", "same", "sleep"):
            with self.subTest(case=case):
                (self.fake / "count").unlink(missing_ok=True)
                path = self.table("codex")
                self.cache("0.163.0")
                self.mode(case)
                before = self.stat(path)
                started = time.monotonic()
                result = self.go(refresh_wait=0.5)
                self.assertLess(time.monotonic() - started, 5)
                self.assertEqual(self.calls(), 1)
                self.assertEqual(self.stat(path), before)
                self.assertEqual(self.baks("codex"), [])
                self.assertEqual((result["status"], result["error"], result["written"]), ("ok", None, []))
                self.assertEqual(result["cli"], {"harness": "codex", "version": "0.171.0", "models": [],
                                                 "kind": "list-from-another-cli", "list_version": "0.163.0",
                                                 "command": None})
                self.assertEqual(model_follow.lines([result]), [self.LINE])
                if case == "sleep":
                    pid = int((self.fake / "pid").read_text())
                    with self.assertRaises(ProcessLookupError):
                        os.kill(pid, 0)

    def test_the_same_line_is_in_the_update_output_through_apply_lines(self):
        self.table("codex")
        self.cache("0.163.0")
        self.mode("fail")
        found = model_follow.apply_lines(["codex"], source_root=self.src, today=TODAY, read_catalog=self.read,
                                         probe=lambda _h: ((0, 171, 0), self.cli))
        self.assertEqual(found, [self.LINE])

    def test_a_newer_list_than_the_installed_cli_is_not_followed_either(self):
        path = self.table("codex")
        self.cache("0.180.0", newer=True)
        self.mode("fail")
        before = self.stat(path)
        result = self.go()
        self.assertEqual((self.stat(path), result["cli"]["list_version"]), (before, "0.180.0"))

    def test_no_refresh_when_versions_agree_the_list_has_none_or_the_cli_is_unknown(self):
        for case, version, probe in (("same", "0.171.0", None), ("no version in the list", None, None),
                                     ("cli version unknown", "0.163.0", lambda _h: (None, self.cli))):
            with self.subTest(case=case):
                (self.fake / "count").unlink(missing_ok=True)
                path = self.table("codex")
                self.cache(version, newer=True)
                self.mode("ok")
                result = self.go(**({"probe": probe} if probe else {}))
                self.assertEqual((self.calls(), result["cli"]), (0, None))
                self.assertEqual(model_config.parse_config(path)["CFG_TIER_DEEP_MODEL"], "deep-2")

    def test_a_harness_without_a_refresh_command_is_not_refreshed(self):
        path = self.table("claude")
        before = self.stat(path)
        catalog = SimpleNamespace(state="available", rows=tuple(self.newer("claude")), client_version="1.0.0")
        result = model_follow.apply_harness("claude", source_root=self.src, today=TODAY,
                                            read_catalog=lambda _h: catalog,
                                            probe=lambda _h: ((2, 0, 0), self.cli))
        self.assertEqual((self.calls(), self.stat(path)), (0, before))
        self.assertEqual(result["cli"]["kind"], "list-from-another-cli")

    def test_the_going_back_of_a_refused_model_goes_on_with_a_list_of_another_version(self):
        path = self.table("codex")
        self.cache("0.171.0", newer=True)
        self.go()
        self.assertEqual(model_config.parse_config(path)["CFG_TIER_DEEP_MODEL"], "deep-2")
        self.cache("0.163.0")  # an older CLI writes the shared list again
        self.mode("fail")
        result = self.go(rejected=["deep-2"])
        self.assertEqual(model_config.parse_config(path)["CFG_TIER_DEEP_MODEL"], "deep-1")
        self.assertTrue(result["reverted"])
        text = model_follow.lines([result])
        self.assertIn(self.LINE, text)
        self.assertTrue([t for t in text if "back to deep-1" in t])

    def test_the_default_probe_asks_the_cli_once_and_the_refresh_runs_once(self):
        path = self.table("codex")
        self.cache("0.163.0")
        self.mode("ok")
        result = model_follow.apply_harness("codex", source_root=self.src, today=TODAY, read_catalog=self.read)
        self.assertEqual(self.calls(), 1)
        self.assertEqual(model_config.parse_config(path)["CFG_TIER_DEEP_MODEL"], "deep-2")
        self.assertIsNone(result["cli"])

    def test_the_view_says_unknown_never_refreshes_and_reads_only(self):
        path = self.table("codex")
        self.cache("0.171.0", newer=True)
        self.go()
        self.assertEqual(model_config.parse_config(path)["CFG_TIER_DEEP_MODEL"], "deep-2")
        self.cache("0.163.0")
        self.mode("ok")
        asked = []

        def probe(harness):
            asked.append(harness)
            return (0, 171, 0), self.cli

        before = self.tree()
        view = model_follow.fleet_view(["codex"], source_root=self.src, today=TODAY,
                                       read_catalog=self.read, probe=probe)
        self.assertEqual((self.tree(), self.calls(), asked), (before, 0, ["codex"]))
        codex = view[0]
        self.assertEqual(codex["status"], "ok")
        self.assertEqual(codex["cli"], {"harness": "codex", "version": "0.171.0", "models": [],
                                        "kind": "list-from-another-cli", "list_version": "0.163.0",
                                        "command": None})
        self.assertEqual(codex["plan"], [])
        cells = {c["target"]: c for c in codex["cells"]}
        self.assertTrue(cells)
        for cell in cells.values():
            self.assertEqual((cell["followable"], cell["reason"], cell["next"]),
                             (None, "list-from-another-cli", None))
        followed = cells["tier/deep"]
        self.assertEqual((followed["state"], followed["why"], followed["since"]["to"]),
                         ("follow", "followed", "deep-2"))

    def test_the_view_gives_a_refusal_back_as_usual_and_nothing_else(self):
        self.cache("0.171.0", newer=True)
        self.table("codex")
        self.go()
        self.cache("0.163.0")
        view = model_follow.fleet_view(["codex"], source_root=self.src, today=TODAY, read_catalog=self.read,
                                       probe=lambda _h: ((0, 171, 0), self.cli), rejected=["deep-2"])
        reverts = [c for c in view[0]["cells"] if c["next"] and c["next"]["branch"] == "revert"]
        self.assertTrue(reverts)
        self.assertTrue(view[0]["plan"])
        self.assertTrue(all(i["action"] == "revert" for i in view[0]["plan"]))
        self.assertEqual(self.calls(), 0)

    def test_a_view_without_a_probe_asks_only_for_the_version(self):
        self.table("codex")
        self.cache("0.163.0")
        self.mode("ok")
        view = model_follow.fleet_view(["codex"], source_root=self.src, today=TODAY, read_catalog=self.read)
        self.assertEqual((self.calls(), view[0]["cli"]["kind"]), (0, "list-from-another-cli"))


if __name__ == "__main__":
    unittest.main()
