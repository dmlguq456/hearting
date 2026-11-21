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
import inspect
import json
import time
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "utilities"))
sys.path.insert(0, str(ROOT / "tools" / "install"))
import model_config  # noqa: E402
import model_follow  # noqa: E402
import model_settings  # noqa: E402
import opencode_runtime  # noqa: E402

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
                 ("xhigh", None, "xhigh", None))
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
        with mock.patch.dict(os.environ, self.env, clear=True):
            return model_follow.survey_harness(
                adapter, source_root=self.src,
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
                self.assertEqual(deep["CFG_TIER_DEEP_FAILOVER_CASCADE"]["swap"],
                                 {f"{name('deep-1')}:xhigh": f"{name('deep-2')}:xhigh"})
                self.assertEqual("CFG_V2_AUTO_FALLBACKS" in deep, adapter == "opencode")
                top = {item["key"]: item for item in entries["tier/top"]["companions"]}
                if adapter == "opencode":
                    self.assertEqual(top, {})
                else:  # the reserved name stays reserved: the new one is added, none removed
                    self.assertEqual(top["CFG_MAIN_SESSION_ONLY_MODELS"]["add"], ["top-2"])
                    self.assertEqual(model_follow.final_lines([entries["tier/top"]])
                                     ["CFG_MAIN_SESSION_ONLY_MODELS"]["to"], "top-1 top-2")

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
                with mock.patch.dict(os.environ, self.env, clear=True):
                    result = model_follow.survey_harness(
                        adapter, source_root=self.src, read_catalog=broken)
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

    def test_a_survey_only_reads_it_writes_nothing_and_says_nothing_about_a_followed_cell(self):
        for adapter in HARNESSES:
            with self.subTest(adapter=adapter):
                path = self.user_file(adapter)
                before = path.read_bytes()
                name = self.names(adapter)
                result = self.run_survey(adapter, [
                    row(name("deep-1")), row(name("deep-2"), version=(2,), efforts=("high",))])
                # Following is `apply_harness`'s work: a survey only plans it.
                self.assertEqual(model_follow.lines([result]), [])
                self.assertEqual(path.read_bytes(), before)
                plan = {item["target"]: item for item in result["plan"]}
                self.assertEqual(plan["profile/deep"]["to"], name("deep-2"))
                self.assertEqual(plan["profile/deep"]["effort"], "high")
                self.assertEqual(plan["tier/deep"]["effort_was"], "xhigh")
                self.assertEqual(model_follow.counts(result)[0], len(plan))

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

    def test_when_no_candidate_is_usable_the_codex_cli_note_still_shows(self):
        self.user_file("codex")
        probe = lambda _harness: ((0, 163, 0), "/usr/bin/codex")  # noqa: E731
        for case, rows in (("only a marked newer model", [row("deep-1", deprecated=True),
                                                         row("deep-2", version=(2,), deprecated=True)]),
                           ("only a hidden newer model", [row("deep-1", deprecated=True),
                                                          row("deep-2", version=(2,), visible=False)]),
                           ("a pointer that is itself marked", [row("deep-1", deprecated=True, upgrade="deep-2"),
                                                                row("deep-2", version=(2,), deprecated=True)]),
                           ("a pointer into another family that is marked",
                            [row("deep-1", deprecated=True, upgrade="n-7"),
                             row("n-7", family="other", version=(7,), deprecated=True)])):
            with self.subTest(case=case):
                found = self.run_survey("codex", rows, client_version="0.163.0", probe=probe)
                self.assertEqual({e["kind"] for e in found["entries"] if e["current"]["model"] == "deep-1"},
                                 {"none"})
                self.assertIn("deep-1", found["cli"]["models"])
                self.assertTrue([t for t in model_follow.lines([found]) if "CLI 0.163.0" in t])

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
        done = SimpleNamespace(stdout="codex-cli 0.163.0\n", stderr="", returncode=0)
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


class CandidateRuleTests(unittest.TestCase):
    """Every candidate, the `upgrade` pointer included, passes one check."""

    def test_a_candidate_marked_previous_generation_is_never_followed(self):
        same = [row("m-1"), row("m-2", version=(2,), deprecated=True)]
        self.assertEqual(model_follow.successor(same, "m-1")["kind"], "none")
        older = [row("m-1"), row("m-2", version=(2,), deprecated=True), row("m-3", version=(1, 5))]
        self.assertEqual(model_follow.successor(older, "m-1")["model"], "m-3")

    def test_the_upgrade_pointer_is_checked_like_any_other_candidate(self):
        for pointer in (row("m-2", version=(2,), deprecated=True),
                        row("m-2", version=(2,), visible=False),
                        row("m-2", version=(2,), efforts=("minimal",)),
                        row("m-2", version=(2,), min_cli="9.0.0"),
                        row("n-7", family="other", version=(7,), deprecated=True),
                        row("n-7", family="other", version=(7,), visible=False),
                        row("n-7", family="other", version=(7,), efforts=("none",)),
                        row("n-7", family="other", version=(7,), min_cli="9.0.0")):
            with self.subTest(pointer=pointer["id"], fields=sorted(k for k, v in pointer.items() if v and k not in ("id", "family", "version"))):
                rows = [row("m-1", upgrade=pointer["id"]), pointer]
                self.assertEqual(model_follow.successor(rows, "m-1", "high")["kind"], "none")
                rows.append(row("m-3", version=(3,)))
                self.assertEqual(model_follow.successor(rows, "m-1", "high")["model"], "m-3")

    def test_a_good_pointer_in_another_family_is_still_a_proposal(self):
        rows = [row("m-1", upgrade="n-7"), row("n-7", family="other", version=(7,), efforts=("high",))]
        found = model_follow.successor(rows, "m-1", "high")
        self.assertEqual((found["kind"], found["model"]), ("proposal", "n-7"))

    def test_the_current_models_own_mark_is_kept(self):
        rows = [row("m-1", deprecated=True), row("m-2", version=(2,), deprecated=True)]
        found = model_follow.successor(rows, "m-1")
        self.assertEqual((found["kind"], found["reason"], found["deprecated"]),
                         ("none", "no-successor", True))


class StorableEffortTests(unittest.TestCase):
    """The effort a line names is one `hearting model set` stores: candidates, the stored
    set and the printed command agree, on every harness and for a tier and a depth line."""

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
        base = {"claude": self.root / "claude", "codex": self.root / "codex",
                "opencode": self.root / "cfg" / "opencode"}
        for adapter in HARNESSES:
            path = base[adapter] / "agent-config" / "models.conf"
            path.parent.mkdir(parents=True)
            path.write_text(conf_text(adapter))

    def survey(self, adapter, efforts):
        prefix = "prov/" if adapter == "opencode" else ""
        rows = [row(prefix + "deep-1"), row(prefix + "deep-2", version=(2,), efforts=efforts)]
        catalog = SimpleNamespace(state="available", rows=tuple(rows), client_version=None)
        with mock.patch.dict(os.environ, self.env, clear=True):
            return model_follow.survey_harness(adapter, source_root=self.src,
                                               read_catalog=lambda _h: catalog,
                                               probe=lambda _h: (None, None))

    def plan(self, adapter, entry, efforts):
        """What `hearting model set` would store for the printed command (nothing is written)."""
        prefix = "prov/" if adapter == "opencode" else ""
        words = entry["command"].split()
        self.assertEqual(words[:3], ["hearting", "model", "set"])
        observation = ({"variants": {prefix + "deep-2": tuple(efforts)}} if adapter == "opencode" else None)
        with mock.patch.dict(os.environ, self.env, clear=True):
            return model_settings.set_model(adapter, words[4], words[5], dry_run=True,
                                            environ=self.env, source_root=self.src,
                                            variant_observation=observation)

    def test_a_model_that_lists_only_unstorable_efforts_is_not_followed(self):
        for adapter in HARNESSES:
            for efforts in (("minimal",), ("none",), ("minimal", "none")):
                with self.subTest(adapter=adapter, efforts=efforts):
                    result = self.survey(adapter, efforts)
                    cells = {e["target"]: e for e in result["entries"]}
                    for target in ("profile/deep", "tier/deep", "tier/balanced-deep"):
                        self.assertEqual((cells[target]["kind"], cells[target]["command"]),
                                         ("none", None))
                    self.assertEqual(model_follow.lines([result]), [])

    def test_every_printed_command_stores_the_effort_its_line_says(self):
        cases = ((("minimal", "medium"), {"profile/deep": "medium", "tier/deep": "medium",
                                          "tier/balanced-deep": "medium"}),
                 (("high", "xhigh"), {"profile/deep": "high", "tier/deep": "xhigh",
                                      "tier/balanced-deep": "high"}),
                 (("none", "low", "high"), {"profile/deep": "high", "tier/deep": "high",
                                            "tier/balanced-deep": "low"}),
                 (("minimal", "high"), {"profile/deep": "high", "tier/deep": "high",
                                        "tier/balanced-deep": "high"}))
        for adapter in HARNESSES:
            for efforts, expected in cases:
                with self.subTest(adapter=adapter, efforts=efforts):
                    result = self.survey(adapter, efforts)
                    cells = {e["target"]: e for e in result["entries"]}
                    for target, effort in expected.items():
                        entry = cells[target]
                        self.assertEqual((entry["kind"], entry["successor"]["effort"]), ("follow", effort))
                        self.assertTrue(entry["command"].endswith("@" + effort), entry["command"])
                        done = self.plan(adapter, entry, efforts)
                        self.assertEqual(done["new_budget"], effort)
                        self.assertEqual(done["new_model"], entry["successor"]["model"])
                    for text in model_follow.lines([result]):
                        self.assertNotIn("minimal", text)

    def test_a_printed_command_is_one_the_store_accepts_for_any_list_of_efforts(self):
        lists = (("runtime-default",), ("runtime-default", "high"), ("high",), ("low", "xhigh"),
                 ("minimal", "runtime-default"), ("max",))
        for adapter in HARNESSES:
            for efforts in lists:
                with self.subTest(adapter=adapter, efforts=efforts):
                    result = self.survey(adapter, efforts)
                    for entry in result["entries"]:
                        if entry["kind"] == "follow":
                            self.plan(adapter, entry, efforts)  # raises when the store refuses it

    def test_a_model_that_lists_only_runtime_default_is_not_followed_at_an_explicit_effort(self):
        result = self.survey("opencode", ("runtime-default",))
        cells = {e["target"]: e for e in result["entries"]}
        for target in ("profile/deep", "tier/deep", "tier/balanced-deep"):
            self.assertEqual((cells[target]["kind"], cells[target]["command"]), ("none", None))
        rows = [row("m-1"), row("m-2", version=(2,), efforts=("runtime-default",))]
        for effort, kind in (("high", "none"), ("runtime-default", "follow")):
            found = model_follow.successor(rows, "m-1", effort, settable=model_follow._SETTABLE_OPENCODE)
            self.assertEqual(found["kind"], kind, effort)

    def test_opencode_runtime_default_stays_a_storable_request(self):
        rows = [row("m-1"), row("m-2", version=(2,), efforts=("minimal",))]
        self.assertEqual(model_follow.successor(rows, "m-1", "runtime-default")["kind"], "none")
        found = model_follow.successor(rows, "m-1", "runtime-default",
                                       settable=model_follow._SETTABLE_OPENCODE)
        self.assertEqual((found["kind"], found["effort"]), ("follow", "runtime-default"))

    def test_a_cell_with_no_effort_is_followed_without_one(self):
        rows = [row("m-1"), row("m-2", version=(2,), efforts=("high",))]
        found = model_follow.successor(rows, "m-1", None)
        self.assertEqual((found["kind"], found["effort"]), ("follow", None))
        self.assertEqual(model_follow._command("codex", {"target": "tier/deep", "successor": {
            "model": "m-2", "effort": None}}), "hearting model set codex tier/deep m-2")


class CliObservationTests(unittest.TestCase):
    """The installed CLI is asked once per survey, and a failed run says nothing."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="mf1-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.counter = self.root / "calls"
        self.counter.write_text("")

    def fake(self, name, body):
        path = self.bin / name
        path.write_text(f"#!/bin/sh\necho x >> {self.counter}\n{body}\n")
        path.chmod(0o700)

    def calls(self):
        return len(self.counter.read_text().split())

    def installed(self, name="claude"):
        with mock.patch.dict(os.environ, {"PATH": str(self.bin)}):
            return model_follow.installed_cli(name)

    def test_a_failed_cli_gives_no_version_whatever_it_printed(self):
        self.fake("claude", "echo 'failed: requires 9.0.0' >&2; exit 1")
        self.assertEqual(self.installed(), (None, str(self.bin / "claude")))
        self.fake("claude", "echo 'claude 9.0.0'; exit 3")
        self.assertEqual(self.installed()[0], None)
        self.fake("claude", "exit 0")
        self.assertEqual(self.installed()[0], None)
        self.fake("claude", "echo garbage")
        self.assertEqual(self.installed()[0], None)
        self.fake("claude", "echo '2.1.5 (Claude Code)'")
        self.assertEqual(self.installed()[0], (2, 1, 5))

    def test_a_failed_cli_neither_admits_a_candidate_nor_confirms_a_codex_note(self):
        helper = SurveyTests("run_survey")
        helper.setUp()
        self.addCleanup(helper.doCleanups)
        self.fake("claude", "echo 'failed: requires 9.0.0' >&2; exit 1")
        helper.user_file("claude")
        rows = [row("deep-1"), row("deep-2", version=(2,), min_cli="2.0.0")]
        found = helper.run_survey("claude", rows, probe=lambda h: self.installed(h))
        self.assertEqual({e["kind"] for e in found["entries"]}, {"none"})
        self.assertIsNone(found["cli"])
        self.fake("codex", "echo 'codex failed 0.163.0' >&2; exit 2")
        helper.user_file("codex")
        found = helper.run_survey("codex", [row("deep-1", deprecated=True)], client_version="0.163.0",
                                  probe=lambda h: self.installed(h))
        self.assertIsNone(found["cli"])

    def test_one_survey_asks_the_cli_once_even_when_it_hangs(self):
        helper = SurveyTests("run_survey")
        helper.setUp()
        self.addCleanup(helper.doCleanups)
        helper.user_file("claude")
        self.fake("claude", "exec /bin/sleep 60")
        rows = [row("deep-1"), row("deep-2", version=(2,), min_cli="2.0.0"),
                row("light-1", family="light"), row("light-2", family="light", version=(2,), min_cli="2.0.0")]
        started = time.monotonic()
        with mock.patch.object(model_follow, "_CLI_TIMEOUT", 1):
            found = helper.run_survey("claude", rows, probe=lambda h: self.installed(h))
        elapsed = time.monotonic() - started
        self.assertEqual(self.calls(), 1)
        self.assertLess(elapsed, 1.9)
        self.assertEqual(model_follow.lines([found]), [])

    def test_a_probe_is_called_once_for_choosing_and_noting_and_a_raise_counts_too(self):
        helper = SurveyTests("run_survey")
        helper.setUp()
        self.addCleanup(helper.doCleanups)
        helper.user_file("claude")
        rows = [row("deep-1"), row("deep-2", version=(2,), min_cli="9.0.0"),
                row("light-1", family="light"), row("light-2", family="light", version=(2,), min_cli="9.0.0")]
        for answer in (((2, 1, 5), "/bin/claude"), (None, None), RuntimeError("fixture")):
            with self.subTest(answer=answer):
                seen = []

                def probe(harness):
                    seen.append(harness)
                    if isinstance(answer, Exception):
                        raise answer
                    return answer

                found = helper.run_survey("claude", rows, probe=probe)
                self.assertEqual(seen, ["claude"])
                self.assertEqual(found["status"], "ok")
        none_needed = helper.run_survey("claude", [row("deep-1"), row("deep-2", version=(2,))],
                                        probe=lambda _h: self.fail("no candidate needs the CLI"))
        self.assertIsNone(none_needed["cli"])


class OneEnvironmentTests(unittest.TestCase):
    def test_a_survey_reads_the_process_environment_and_never_swaps_it(self):
        self.assertNotIn("environ", inspect.signature(model_follow.survey_harness).parameters)
        helper = SurveyTests("run_survey")
        helper.setUp()
        self.addCleanup(helper.doCleanups)
        helper.user_file("codex")
        seen = []

        def reader(_harness):
            seen.append(dict(os.environ))
            return SimpleNamespace(state="available", rows=(row("deep-1"),), client_version=None)

        with mock.patch.dict(os.environ, helper.env, clear=True):
            before = dict(os.environ)
            model_follow.survey_harness("codex", source_root=helper.src, read_catalog=reader,
                                        probe=lambda _h: (None, None))
            self.assertEqual(dict(os.environ), before)
        self.assertEqual(seen, [before])


def conf_with(adapter, overrides):
    """`conf_text(adapter)` with some lines replaced (or appended), values written as given."""
    kept, seen = [], set()
    for line in conf_text(adapter).splitlines():
        key = line.partition("=")[0]
        if key in overrides:
            line, seen = f"{key}={overrides[key]}", seen | {key}
        kept.append(line)
    kept += [f"{key}={value}" for key, value in overrides.items() if key not in seen]
    return "\n".join(kept) + "\n"


def accepted(item, rows):
    """True when `model:effort` names an effort its model lists (or `runtime-default`)."""
    model, _, effort = item.partition(":")
    listed = next(r for r in rows if r["id"] == model).get("efforts")
    return effort == "runtime-default" or not listed or effort in listed


class LinesMoveWithTheCellTests(unittest.TestCase):
    """A cell follows only when its model can take every line that moves with it, at
    efforts it lists; what `final_lines` gives is written as it is, on every harness."""

    ELM = [row("p/elm-1", family="elm"), row("p/elm-2", family="elm", version=(2,), efforts=("high",)),
           row("p/elm-3", family="elm", version=(3,), efforts=("minimal",))]
    REPRO = {"CFG_TIER_MINI_MODEL": "p/elm-1", "CFG_TIER_MINI_VARIANT": "runtime-default",
             "CFG_MODEL_PROFILE_DEEP": "model/p/elm-1:high",
             "CFG_TIER_DEEP_FAILOVER_CASCADE": '"p/elm-1:high p/elm-1:runtime-default p/elm-1:high"'}

    def survey(self, adapter, rows, text):
        helper = SurveyTests("run_survey")
        helper.setUp()
        self.addCleanup(helper.doCleanups)
        path = helper.user_file(adapter, text)
        self.assertEqual(path.read_text(), text)
        return helper.run_survey(adapter, rows), path

    def write_back(self, path, final):
        """Write each final value into the (isolated) user file and read the file again."""
        values = model_config.parse_config(path)
        lines = []
        for line in path.read_text().splitlines():
            key = line.partition("=")[0]
            lines.append(f'{key}="{final[key]["to"]}"' if key in final else line)
        path.write_text("\n".join(lines) + "\n")
        return model_config.parse_config(path), values

    def test_a_cell_whose_main_effort_is_runtime_default_follows_no_further_than_its_lines(self):
        result, path = self.survey("opencode", self.ELM, conf_with("opencode", self.REPRO))
        cells = {e["target"]: e for e in result["entries"]}
        mini = cells["tier/mini"]  # the newest model lists only `minimal`; the cascade says `high`
        self.assertEqual((mini["kind"], mini["reason"], mini["successor"], mini["companions"],
                          mini["command"]), ("none", "lines-do-not-fit", None, [], None))
        self.assertEqual(cells["profile/deep"]["successor"]["model"], "p/elm-2")
        follows = [e for e in result["entries"] if e["kind"] == "follow"]
        written, _before = self.write_back(path, model_follow.final_lines(follows))
        cascade = written[model_follow.CASCADE_KEY].split()
        self.assertEqual(cascade, ["p/elm-2:high", "p/elm-2:runtime-default", "p/elm-2:high"])
        self.assertTrue(all(accepted(item, self.ELM) for item in cascade), cascade)
        self.assertTrue(all(opencode_runtime.variant_support(
            *item.split(":"), {"p/elm-2": ("high",)}.get(item.split(":")[0])) != "unsupported"
            for item in cascade))
        self.assertEqual([t for t in model_follow.lines([result]) if "elm-3" in t], [])

    def test_two_cells_that_pick_different_models_leave_one_accepted_item_whatever_the_order(self):
        text = conf_with("opencode", {**self.REPRO, "CFG_TIER_DEEP_FAILOVER_CASCADE":
                                      '"p/elm-1:runtime-default p/elm-1:runtime-default"',
                                      "CFG_V2_AUTO_FALLBACKS": '"p/elm-1:runtime-default"'})
        result, path = self.survey("opencode", self.ELM, text)
        follows = [e for e in result["entries"] if e["kind"] == "follow"]
        models = {e["target"]: e["successor"]["model"] for e in follows}
        self.assertEqual((models["tier/mini"], models["profile/deep"]), ("p/elm-3", "p/elm-2"))
        forward = model_follow.final_lines(follows)
        self.assertEqual(model_follow.final_lines(follows[::-1]), forward)  # the first cell in the file wins
        written, _before = self.write_back(path, forward)
        for key in (model_follow.CASCADE_KEY, model_follow.OPENCODE_FALLBACK_KEY):
            items = written[key].split()
            self.assertEqual(set(items), {"p/elm-3:runtime-default"})
            self.assertTrue(all(accepted(item, self.ELM) for item in items), items)

    def test_a_line_item_the_new_model_does_not_list_stops_the_cell_on_every_harness(self):
        for adapter in HARNESSES:
            with self.subTest(adapter=adapter):
                name = SurveyTests.names(adapter)
                cascade = f'"{name("deep-1")}:xhigh {name("deep-1")}:minimal"'
                text = conf_with(adapter, {"CFG_TIER_DEEP_FAILOVER_CASCADE": cascade})
                rows = lambda efforts: [row(name("deep-1")), row(name("deep-2"), version=(2,), efforts=efforts)]
                result, path = self.survey(adapter, rows(("high",)), text)
                for entry in result["entries"]:
                    if entry["current"]["model"] == name("deep-1"):
                        self.assertEqual((entry["kind"], entry["reason"], entry["command"]),
                                         ("none", "lines-do-not-fit", None), entry["target"])
                self.assertEqual(model_follow.lines([result]), [])
                result, path = self.survey(adapter, rows(("minimal", "high")), text)
                follows = [e for e in result["entries"] if e["kind"] == "follow"]
                self.assertTrue(follows)
                written, _before = self.write_back(path, model_follow.final_lines(follows))
                items = written[model_follow.CASCADE_KEY].split()
                self.assertEqual(items, [f"{name('deep-2')}:high", f"{name('deep-2')}:minimal"])

    def test_the_survey_order_decides_so_entries_may_be_given_in_any_order(self):
        text = conf_with("codex", {"CFG_TIER_DEEP_FAILOVER_CASCADE": '"deep-1:xhigh light-1:medium"',
                                   "CFG_MAIN_SESSION_ONLY_MODELS": '"deep-1 light-1"'})
        rows = [row("deep-1", family="d"), row("deep-2", family="d", version=(2,)),
                row("light-1", family="l"), row("light-2", family="l", version=(2,))]
        result, _path = self.survey("codex", rows, text)
        entries = [e for e in result["entries"] if e["companions"]]
        self.assertGreater(len({e["order"] for e in entries}), 1)
        forward = model_follow.final_lines(entries)
        self.assertEqual(model_follow.final_lines(entries[::-1]), forward)
        self.assertEqual(forward["CFG_MAIN_SESSION_ONLY_MODELS"]["to"], "deep-1 light-1 deep-2 light-2")


class FinalLineTests(unittest.TestCase):
    """Cells give small edits; one final value per line comes from `final_lines`."""

    def entries(self, adapter="codex", values=None, pairs=(("a-1", "a-2"), ("b-1", "b-2"))):
        values = values or {
            model_follow.CASCADE_KEY: "a-1:high b-1:high a-1-mini:low",
            model_follow.OPENCODE_FALLBACK_KEY: "a-1:runtime-default b-1:medium",
            "CFG_MAIN_SESSION_ONLY_MODELS": "a-1 b-1"}
        return [{"companions": model_follow.companions(adapter, values, old, new, ("medium", "high"))}
                for old, new in pairs]

    def test_a_cell_lists_only_its_own_swap_never_a_whole_line(self):
        first = self.entries()[0]["companions"]
        self.assertEqual({c["key"]: c.get("swap") or c.get("add") for c in first},
                         {model_follow.CASCADE_KEY: {"a-1:high": "a-2:high"},
                          "CFG_MAIN_SESSION_ONLY_MODELS": ["a-2"]})
        self.assertNotIn("to", first[0])

    def test_two_families_changing_together_give_one_final_value_per_line(self):
        final = model_follow.final_lines(self.entries())
        self.assertEqual(final[model_follow.CASCADE_KEY],
                         {"from": "a-1:high b-1:high a-1-mini:low", "to": "a-2:high b-2:high a-1-mini:low"})
        self.assertEqual(final["CFG_MAIN_SESSION_ONLY_MODELS"],
                         {"from": "a-1 b-1", "to": "a-1 b-1 a-2 b-2"})
        self.assertNotIn(model_follow.OPENCODE_FALLBACK_KEY, final)

    def test_opencode_fallbacks_merge_too_and_keep_runtime_default(self):
        entries = self.entries("opencode")
        final = model_follow.final_lines(entries)
        self.assertEqual(final[model_follow.OPENCODE_FALLBACK_KEY]["to"], "a-2:runtime-default b-2:medium")
        self.assertNotIn("CFG_MAIN_SESSION_ONLY_MODELS", final)

    def test_order_does_not_matter_and_a_repeated_cell_changes_nothing_more(self):
        entries = self.entries()
        forward = model_follow.final_lines(entries)
        self.assertEqual(model_follow.final_lines(entries[::-1])[model_follow.CASCADE_KEY]["to"],
                         forward[model_follow.CASCADE_KEY]["to"])
        self.assertEqual(model_follow.final_lines([*entries, *entries]), forward)

    def test_an_item_two_cells_map_differently_takes_the_first_in_file_order(self):
        values = {model_follow.CASCADE_KEY: "a-1:high"}
        one = {"companions": model_follow.companions("codex", values, "a-1", "a-2", None)}
        two = {"companions": model_follow.companions("codex", values, "a-1", "a-3", None)}
        self.assertEqual(model_follow.final_lines([one, two])[model_follow.CASCADE_KEY]["to"], "a-2:high")

    def test_a_survey_with_two_families_changing_gives_the_merged_cascade(self):
        for adapter in HARNESSES:
            with self.subTest(adapter=adapter):
                helper = SurveyTests("run_survey")
                helper.setUp()
                self.addCleanup(helper.doCleanups)
                helper.user_file(adapter)
                name = helper.names(adapter)
                result = helper.run_survey(adapter, [
                    row(name("deep-1")), row(name("deep-2"), version=(2,), efforts=("medium", "xhigh")),
                    row(name("light-1"), family="light"),
                    row(name("light-2"), family="light", version=(2,), efforts=("low", "medium"))])
                final = model_follow.final_lines(result["entries"])
                self.assertEqual(final[model_follow.CASCADE_KEY]["to"],
                                 f"{name('deep-2')}:xhigh {name('light-2')}:medium")


if __name__ == "__main__":
    unittest.main()
