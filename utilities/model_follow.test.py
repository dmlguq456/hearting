#!/usr/bin/env python3
"""The model-follow rule: rows in, one entry per table cell out; nothing is written.

Shared-rule fixtures use made-up model names on purpose. Real vendor names, and the
patterns that turn them into rows, are tested with each harness's catalog reader
(`tools/fleet/tests/test_explorer_catalog.py`). Every case runs on all three harnesses.
"""
from __future__ import annotations
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import hashlib
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "utilities"))
import model_follow  # noqa: E402

HARNESSES = ("claude", "codex", "opencode")


def row(model, family="fam", version=(1,), **fields):
    base = {"id": model, "family": family, "version": version, "visible": True,
            "deprecated": False, "efforts": None, "cost": None, "upgrade": None, "min_cli": None}
    return {**base, **fields}


class SuccessorRuleTests(unittest.TestCase):
    def test_newer_same_family_is_followed_and_the_newest_wins(self):
        rows = [row("m-1"), row("m-2", version=(2,)), row("m-3", version=(2, 5)),
                row("other-9", family="other", version=(9,))]
        found = model_follow.successor(rows, "m-1", "high")
        self.assertEqual((found["kind"], found["model"], found["effort"]), ("follow", "m-3", "high"))

    def test_nothing_newer_is_none_and_keeps_the_previous_generation_mark(self):
        rows = [row("m-1", deprecated=True), row("m-0", version=(0, 9))]
        found = model_follow.successor(rows, "m-1", "high")
        self.assertEqual((found["kind"], found["reason"], found["deprecated"]),
                         ("none", "no-successor", True))

    def test_only_models_visible_in_this_account_count(self):
        rows = [row("m-1"), row("m-2", version=(2,), visible=False)]
        self.assertEqual(model_follow.successor(rows, "m-1")["kind"], "none")

    def test_names_without_a_pattern_or_a_listing_are_not_followed(self):
        rows = [row("m-1", family=None, version=None), row("m-2", version=(2,))]
        self.assertEqual(model_follow.successor(rows, "m-1")["reason"], "no-pattern")
        self.assertEqual(model_follow.successor(rows, "unlisted")["reason"], "not-in-list")

    def test_upgrade_in_the_same_family_comes_first(self):
        rows = [row("m-1", upgrade="m-2"), row("m-2", version=(2,)), row("m-3", version=(3,))]
        found = model_follow.successor(rows, "m-1")
        self.assertEqual((found["kind"], found["model"]), ("follow", "m-2"))

    def test_upgrade_into_another_family_is_only_a_proposal(self):
        rows = [row("m-1", upgrade="n-7"), row("n-7", family="other", version=(7,)),
                row("m-2", version=(2,))]
        found = model_follow.successor(rows, "m-1")
        self.assertEqual((found["kind"], found["model"], found["reason"]),
                         ("proposal", "n-7", "other family"))

    def test_upgrade_that_this_account_does_not_list_is_ignored(self):
        rows = [row("m-1", upgrade="n-7"), row("n-7", family="other", version=(7,), visible=False),
                row("m-2", version=(2,))]
        self.assertEqual(model_follow.successor(rows, "m-1")["model"], "m-2")

    def test_a_price_more_than_double_or_below_half_is_a_proposal(self):
        for cost, kind in ((0.3, "follow"), (0.4, "follow"), (0.41, "proposal"), (0.1, "follow"),
                           (0.09, "proposal"), (0.0, "proposal")):
            with self.subTest(cost=cost):
                rows = [row("m-1", cost=0.2), row("m-2", version=(2,), cost=cost)]
                self.assertEqual(model_follow.successor(rows, "m-1")["kind"], kind)
        free = [row("m-1", cost=0.0), row("m-2", version=(2,), cost=0.0)]
        self.assertEqual(model_follow.successor(free, "m-1")["kind"], "follow")
        unknown = [row("m-1", cost=0.2), row("m-2", version=(2,), cost=None)]
        self.assertEqual(model_follow.successor(unknown, "m-1")["kind"], "follow")

    def test_effort_stays_or_drops_to_the_value_just_below(self):
        low = ("low", "medium", "high")
        cases = (("high", low, "high", None), ("xhigh", low, "high", "xhigh"),
                 ("max", low, "high", "max"), ("low", ("medium", "high"), "medium", "low"),
                 ("xhigh", None, "xhigh", None), ("odd", low, "odd", None))
        for effort, efforts, expected, was in cases:
            with self.subTest(effort=effort, efforts=efforts):
                rows = [row("m-1"), row("m-2", version=(2,), efforts=efforts)]
                found = model_follow.successor(rows, "m-1", effort)
                self.assertEqual((found["effort"], found["fit_from"]), (expected, was))

    def test_a_model_needing_a_newer_cli_is_followed_only_when_the_cli_is_known_to_allow_it(self):
        rows = [row("m-1"), row("m-2", version=(2,), min_cli="3.0.0")]
        for have, expected in (((3, 0, 1), "follow"), ((2, 9, 9), "none"), (None, "none")):
            with self.subTest(have=have):
                found = model_follow.successor(rows, "m-1", installed=lambda have=have: have)
                self.assertEqual(found["kind"], expected)


def conf_text(adapter, *, deep="deep-1", light="light-1", top="top-1"):
    effort = "VARIANT" if adapter == "opencode" else "EFFORT"
    prefix = "prov/" if adapter == "opencode" else ""
    lines = ["CFG_MAIN_SESSION_ONLY_MODELS=\"%s%s\"" % (prefix, top)] if adapter != "opencode" else []
    for tier, model, level in (("TOP", top, "xhigh"), ("DEEP", deep, "xhigh"),
                               ("BALANCED_DEEP", deep, "medium"), ("LIGHT", light, "medium"),
                               ("MINI", light, "low")):
        lines += [f"CFG_TIER_{tier}_MODEL={prefix}{model}", f"CFG_TIER_{tier}_{effort}={level}"]
    lines += [f"CFG_MODEL_PROFILE_DEEP=model/{prefix}{deep}:high",
              "CFG_MODEL_PROFILE_BALANCED_DEEP=balanced-deep:medium",
              "CFG_MODEL_PROFILE_BALANCED=light:high", "CFG_MODEL_PROFILE_LIGHT=light:medium",
              "CFG_MODEL_PROFILE_MINI=mini:low", "CFG_MODEL_PROFILE_TOP=top:xhigh",
              "CFG_MODEL_PROFILE_GRANULARITY=full", "CFG_TIER_DEEP_FAILOVER=light",
              f"CFG_TIER_DEEP_FAILOVER_CASCADE=\"{prefix}{deep}:xhigh {prefix}{light}:medium\""]
    if adapter == "opencode":
        lines.append(f"CFG_V2_AUTO_FALLBACKS=\"{prefix}{deep}:runtime-default\"")
    return "\n".join(lines) + "\n"


class SurveyTests(unittest.TestCase):
    """One cell per entry, per harness, on a fixture home; nothing is written."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="mf1-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        for adapter in HARNESSES:
            config = self.root / "src" / "adapters" / adapter / "config"
            config.mkdir(parents=True)
            (config / "models.conf").write_text(conf_text(adapter))
        self.env = {"HOME": str(self.root), "CLAUDE_CONFIG_DIR": str(self.root / "claude"),
                    "CODEX_HOME": str(self.root / "codex"), "XDG_CONFIG_HOME": str(self.root / "cfg"),
                    "PATH": ""}
        self.src = str(self.root / "src")

    def user_file(self, adapter, text=None):
        base = {"claude": self.root / "claude", "codex": self.root / "codex",
                "opencode": self.root / "cfg" / "opencode"}[adapter]
        path = base / "agent-config" / "models.conf"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text if text is not None else conf_text(adapter))
        return path

    def run_survey(self, adapter, rows, *, client_version=None, probe=None, state="available"):
        catalog = SimpleNamespace(state=state, rows=tuple(rows), client_version=client_version)
        return model_follow.survey_harness(
            adapter, environ=self.env, source_root=self.src,
            read_catalog=lambda _harness: catalog,
            probe=probe or (lambda _harness: (None, None)))

    @staticmethod
    def names(adapter):
        prefix = "prov/" if adapter == "opencode" else ""
        return lambda model: prefix + model

    def test_depth_line_and_tier_line_both_become_follow_targets(self):
        for adapter in HARNESSES:
            with self.subTest(adapter=adapter):
                self.user_file(adapter)
                name = self.names(adapter)
                result = self.run_survey(adapter, [
                    row(name("deep-1")), row(name("deep-2"), version=(2,), efforts=("medium", "high")),
                    row(name("light-1"), family="light"), row(name("top-1"), family="top")])
                kinds = {entry["target"]: entry for entry in result["entries"]}
                self.assertEqual(kinds["profile/deep"]["kind"], "follow")
                self.assertEqual(kinds["tier/deep"]["kind"], "follow")
                self.assertEqual(kinds["tier/balanced-deep"]["kind"], "follow")
                self.assertEqual(kinds["tier/light"]["kind"], "none")
                self.assertEqual(kinds["profile/deep"]["successor"],
                                 {"model": name("deep-2"), "effort": "high", "fit_from": None})
                # the tier line asks for xhigh; the new model's top rung is high
                self.assertEqual(kinds["tier/deep"]["successor"]["effort"], "high")
                self.assertEqual(kinds["tier/deep"]["successor"]["fit_from"], "xhigh")
                self.assertEqual(kinds["profile/deep"]["command"],
                                 f"hearting model set {adapter} profile/deep {name('deep-2')}@high")
                self.assertEqual(kinds["tier/balanced-deep"]["command"],
                                 f"hearting model set {adapter} tier/balanced-deep {name('deep-2')}@medium")

    def test_lines_that_must_change_with_a_cell_are_listed_with_it(self):
        for adapter in HARNESSES:
            with self.subTest(adapter=adapter):
                self.user_file(adapter)
                name = self.names(adapter)
                result = self.run_survey(adapter, [
                    row(name("deep-1")), row(name("deep-2"), version=(2,), efforts=("high", "xhigh")),
                    row(name("top-1"), family="top"),
                    row(name("top-2"), family="top", version=(2,))])
                entries = {entry["target"]: entry for entry in result["entries"]}
                deep = {item["key"]: item for item in entries["tier/deep"]["companions"]}
                self.assertEqual(deep["CFG_TIER_DEEP_FAILOVER_CASCADE"]["to"],
                                 f"{name('deep-2')}:xhigh {name('light-1')}:medium")
                self.assertEqual("CFG_V2_AUTO_FALLBACKS" in deep, adapter == "opencode")
                top = {item["key"]: item for item in entries["tier/top"]["companions"]}
                if adapter == "opencode":
                    self.assertEqual(top, {})
                else:  # the reserved name stays reserved: the new one is added, none removed
                    self.assertEqual(top["CFG_MAIN_SESSION_ONLY_MODELS"]["to"], "top-1 top-2")
                    self.assertEqual(top["CFG_MAIN_SESSION_ONLY_MODELS"]["add"], "top-2")

    def test_user_file_is_not_touched_by_a_survey(self):
        for adapter in HARNESSES:
            with self.subTest(adapter=adapter):
                path = self.user_file(adapter)
                name = self.names(adapter)
                before = hashlib.sha256(path.read_bytes()).hexdigest()
                listing = sorted(p.name for p in path.parent.iterdir())
                self.run_survey(adapter, [row(name("deep-1")), row(name("deep-2"), version=(2,))])
                self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), before)
                self.assertEqual(sorted(p.name for p in path.parent.iterdir()), listing)

    def test_a_list_or_file_that_cannot_be_read_is_skipped_quietly(self):
        for adapter in HARNESSES:
            name = self.names(adapter)
            with self.subTest(adapter=adapter, case="no user file"):
                self.assertEqual(self.run_survey(adapter, [row(name("deep-1"))])["reason"],
                                 "no-user-file")
            self.user_file(adapter)
            with self.subTest(adapter=adapter, case="no list"):
                self.assertEqual(self.run_survey(adapter, [], state="unknown")["reason"], "no-catalog")
            with self.subTest(adapter=adapter, case="empty rows"):
                self.assertEqual(self.run_survey(adapter, [])["status"], "skipped")
            with self.subTest(adapter=adapter, case="reader raises"):
                def broken(_harness):
                    raise ValueError("fixture")
                result = model_follow.survey_harness(
                    adapter, environ=self.env, source_root=self.src, read_catalog=broken)
                self.assertEqual(result["status"], "skipped")
                self.assertEqual(model_follow.lines([result]), [])
            with self.subTest(adapter=adapter, case="broken file"):
                self.user_file(adapter, "not a config\n")
                self.assertEqual(self.run_survey(adapter, [row(name("deep-1"))])["reason"],
                                 "no-user-file")

    def test_unlisted_models_and_names_without_a_pattern_give_no_lines(self):
        for adapter in HARNESSES:
            with self.subTest(adapter=adapter):
                self.user_file(adapter)
                name = self.names(adapter)
                result = self.run_survey(adapter, [
                    row(name("deep-1"), family=None, version=None),
                    row(name("deep-2"), version=(2,)), row(name("someone-else"))])
                self.assertEqual({e["kind"] for e in result["entries"]}, {"none"})
                self.assertEqual(model_follow.lines([result]), [])
                self.assertEqual(model_follow.counts(result), (0, 0))

    def test_other_family_and_price_become_proposals_with_the_reason(self):
        for adapter in HARNESSES:
            with self.subTest(adapter=adapter):
                self.user_file(adapter)
                name = self.names(adapter)
                result = self.run_survey(adapter, [
                    row(name("deep-1"), upgrade=name("zed-9")),
                    row(name("zed-9"), family="zed", version=(9,)),
                    row(name("light-1"), family="light", cost=0.1),
                    row(name("light-2"), family="light", version=(2,), cost=0.9)])
                got = {e["target"]: (e["kind"], e["reason"]) for e in result["entries"]}
                self.assertEqual(got["profile/deep"], ("proposal", "other family"))
                self.assertEqual(got["tier/light"], ("proposal", "price x9.0"))
                self.assertEqual(model_follow.counts(result), (0, 5))
                text = "\n".join(model_follow.lines([result]))
                self.assertIn(f"model follow: proposal {adapter} profile/deep {name('deep-1')} -> "
                              f"{name('zed-9')} (other family) · hearting model set {adapter} "
                              f"profile/deep {name('zed-9')}@high", text)
                self.assertNotIn("followed", text)

    def test_result_lines_say_newer_and_never_followed(self):
        for adapter in HARNESSES:
            with self.subTest(adapter=adapter):
                self.user_file(adapter)
                name = self.names(adapter)
                result = self.run_survey(adapter, [
                    row(name("deep-1")), row(name("deep-2"), version=(2,), efforts=("high",))])
                text = model_follow.lines([result])
                self.assertIn(f"model follow: newer {adapter} profile/deep {name('deep-1')} -> "
                              f"{name('deep-2')} (high) · hearting model set {adapter} "
                              f"profile/deep {name('deep-2')}@high", text)
                self.assertIn(f"model follow: newer {adapter} tier/deep {name('deep-1')} -> "
                              f"{name('deep-2')} (high, was xhigh) · hearting model set {adapter} "
                              f"tier/deep {name('deep-2')}@high", text)
                self.assertFalse([t for t in text if "followed" in t])

    def test_codex_cli_note_needs_a_matching_list_version_a_mark_and_no_successor(self):
        self.user_file("codex")
        rows = [row("deep-1", deprecated=True), row("light-1", family="light", deprecated=True)]
        probe = lambda _harness: ((0, 163, 0), "/usr/bin/codex")  # noqa: E731
        result = self.run_survey("codex", rows, client_version="0.163.0", probe=probe)
        self.assertEqual(result["cli"]["models"], ["deep-1", "light-1"])
        self.assertEqual(model_follow.lines([result])[-1],
                         "model follow: codex CLI 0.163.0 lists deep-1, light-1 as previous "
                         "generation with no successor · update the Codex CLI yourself")
        for case, kwargs in (
                ("list is another CLI version", {"client_version": "0.170.0", "probe": probe}),
                ("no list version", {"client_version": None, "probe": probe}),
                ("installed version unknown", {"client_version": "0.163.0"}),
                ("not marked", {"client_version": "0.163.0", "probe": probe,
                                "rows": [row("deep-1"), row("light-1", family="light")]}),
                ("a successor is listed", {"client_version": "0.163.0", "probe": probe,
                                           "rows": [row("deep-1", deprecated=True),
                                                    row("deep-2", version=(2,))]})):
            with self.subTest(case=case):
                kwargs = dict(kwargs)
                found = self.run_survey("codex", kwargs.pop("rows", rows), **kwargs)
                self.assertIsNone(found["cli"])
                self.assertFalse([t for t in model_follow.lines([found]) if "CLI" in t])

    def test_claude_cli_note_names_the_model_and_the_needed_version(self):
        self.user_file("claude")
        rows = [row("deep-1"), row("deep-2", version=(2,), min_cli="9.1.0"),
                row("light-1", family="light", min_cli="1.0.0")]
        result = self.run_survey("claude", rows,
                                 probe=lambda _h: ((2, 1, 5), "/home/u/.local/share/claude/versions/2.1.5"))
        self.assertEqual(result["cli"]["models"], [("deep-2", "9.1.0")])
        self.assertEqual(model_follow.lines([result])[-1],
                         "model follow: claude CLI 2.1.5 is older than deep-2 (needs 9.1.0) · claude update")
        new = self.run_survey("claude", rows, probe=lambda _h: ((9, 1, 0), None))
        self.assertIsNone(new["cli"])
        self.assertEqual({e["target"]: e["kind"] for e in new["entries"]}["tier/deep"], "follow")

    def test_opencode_never_gets_a_cli_note(self):
        self.user_file("opencode")
        rows = [row("prov/deep-1", deprecated=True)]
        result = self.run_survey("opencode", rows, client_version="2.0.0",
                                 probe=lambda _h: ((2, 0, 0), None))
        self.assertIsNone(result["cli"])

    def test_the_upgrade_command_is_given_only_when_the_install_method_is_evident(self):
        self.assertEqual(model_follow.upgrade_command("codex", "/x/lib/node_modules/@openai/codex/bin/codex"),
                         "npm i -g @openai/codex")
        self.assertEqual(model_follow.upgrade_command("codex", "/opt/homebrew/Cellar/codex/1/bin/codex"),
                         "brew upgrade codex")
        self.assertIsNone(model_follow.upgrade_command("codex", "/usr/local/bin/codex"))
        self.assertIsNone(model_follow.upgrade_command("codex", None))
        self.assertIsNone(model_follow.upgrade_command("claude", "/usr/local/bin/claude"))

    def test_installed_version_comes_from_one_bounded_cli_call_and_failure_is_silent(self):
        done = SimpleNamespace(stdout="codex-cli 0.163.0\n", stderr="")
        with mock.patch.object(model_follow.shutil, "which", return_value="/bin/codex"), \
                mock.patch.object(model_follow.subprocess, "run", return_value=done) as run:
            self.assertEqual(model_follow.installed_cli("codex"), ((0, 163, 0), "/bin/codex"))
        self.assertEqual(run.call_args.args[0], ["/bin/codex", "--version"])
        self.assertLessEqual(run.call_args.kwargs["timeout"], 5)
        with mock.patch.object(model_follow.shutil, "which", return_value="/bin/codex"), \
                mock.patch.object(model_follow.subprocess, "run", side_effect=OSError("x")):
            self.assertEqual(model_follow.installed_cli("codex")[0], None)
        with mock.patch.object(model_follow.shutil, "which", return_value=None):
            self.assertEqual(model_follow.installed_cli("codex"), (None, None))


if __name__ == "__main__":
    unittest.main()
