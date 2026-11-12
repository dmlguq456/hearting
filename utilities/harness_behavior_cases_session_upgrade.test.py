#!/usr/bin/env python3
"""Offline checks for the live session-upgrade scenarios (no TUI is launched)."""
from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness_behavior_scenarios as hb  # noqa: E402
import harness_behavior_cases_session_upgrade as su  # noqa: E402

NAMES = ("session-upgrade", "session-upgrade-scrollback", "session-upgrade-draft",
         "session-upgrade-turn", "session-upgrade-question", "session-upgrade-page",
         "session-upgrade-delivery", "session-upgrade-fail", "route-resume")


def fixture_env(root: Path) -> dict:
    return {"CLAUDE_CONFIG_DIR": str(root / ".claude"), "CODEX_HOME": str(root / ".codex"),
            "XDG_CONFIG_HOME": str(root / ".config"), "XDG_STATE_HOME": str(root / "state")}


class RegistrationTest(unittest.TestCase):
    def test_every_scenario_is_registered_once_with_a_summary(self):
        self.assertEqual(tuple(cls.name for cls in su.SCENARIOS), NAMES)
        hb._load_extra()
        for name in NAMES:
            self.assertIn(name, hb.SCENARIOS)
            self.assertTrue(hb.SCENARIOS[name].summary)
        # No other companion registers one of these names.
        owners = [cls for cls in hb.SCENARIOS.values() if cls.name in NAMES]
        self.assertEqual(len(owners), len(NAMES))

    def test_held_variants_and_fail_variant_are_marked(self):
        held = {cls.name for cls in su.SCENARIOS if getattr(cls, "hold", False)}
        self.assertEqual(held, {"session-upgrade-draft", "session-upgrade-turn",
                                "session-upgrade-question", "session-upgrade-page",
                                "session-upgrade-delivery"})
        self.assertTrue(su.FailScenario.fail)
        self.assertTrue(su.ScrollbackScenario.scrollback)


class ArgvTest(unittest.TestCase):
    def test_link_entry_is_under_the_install_link_per_harness(self):
        with tempfile.TemporaryDirectory() as raw:
            env = fixture_env(Path(raw))
            tail = Path("hearting/utilities/session-shell/entry.py")
            self.assertEqual(su.link_entry("claude", env), Path(raw) / ".claude" / tail)
            self.assertEqual(su.link_entry("codex", env), Path(raw) / ".codex" / tail)
            self.assertEqual(su.link_entry("opencode", env),
                             Path(raw) / ".config" / "opencode" / tail)

    def test_upgrade_argv_runs_the_link_entry_not_the_source_entry(self):
        entry = Path("/fx/.claude/hearting/utilities/session-shell/entry.py")
        for harness in hb.HARNESSES:
            native = hb._native_argv(harness, "/bin/native")
            argv = su.upgrade_argv(harness, native, entry)
            self.assertEqual(argv[:3], [sys.executable, str(entry), harness])
            self.assertEqual(argv[3:], native)
            self.assertNotIn(str(hb.SOURCE), " ".join(argv[1:2]))

    def test_resume_argv_names_the_same_session_for_each_harness(self):
        self.assertEqual(su.resume_native("claude", "c", "sid1", "m"),
                         ["c", "--model", "m", "--resume", "sid1"])
        self.assertEqual(su.resume_native("codex", "x", "sid1", "m"),
                         ["x", "--no-alt-screen", "resume", "sid1"])
        self.assertEqual(su.resume_native("opencode", "o", "ses_1", "m"),
                         ["o", "--standalone", "-s", "ses_1"])


class DigestTest(unittest.TestCase):
    def test_same_screen_same_digest_and_a_changed_row_differs(self):
        a = "hello\nworld   \n\n"
        self.assertEqual(su.screen_digest(a)["sha"], su.screen_digest("hello\nworld")["sha"])
        self.assertNotEqual(su.screen_digest(a)["sha"], su.screen_digest("hello\nworlds")["sha"])

    def test_time_only_rows_are_dropped_and_counted(self):
        base = "answer one\nprompt >"
        noisy = "answer one\n✻ Thinking… (12s · esc to interrupt)\nclock 10:42:07\nprompt >"
        plain, other = su.screen_digest(base), su.screen_digest(noisy)
        self.assertEqual(plain["sha"], other["sha"])
        self.assertEqual(other["dropped"], 2)
        self.assertIn("spinner", su.VOLATILE_RULE)

    def test_waiting_note_and_panel_are_left_out_of_a_held_comparison(self):
        before = "native row   │ ┌ idle ┐\n❯           │ │ ◑ to answer 1 │\n"
        held = "native row   │ ┌ idle ┐\n❯  New release waiting │ │ ◑ to answer 1  ⚠ │\n"
        one = su.screen_digest(before, blank=su.WAIT_NOTE, native=True)
        two = su.screen_digest(held, blank=su.WAIT_NOTE, native=True)
        self.assertEqual(one["sha"], two["sha"])
        self.assertNotEqual(su.screen_digest(before)["sha"], su.screen_digest(held)["sha"])
        # A real change on the native side still differs.
        changed = held.replace("❯", "X")
        self.assertNotEqual(one["sha"], su.screen_digest(changed, blank=su.WAIT_NOTE, native=True)["sha"])

    def test_tool_timers_are_time_only_rows(self):
        base = "Scenario step\nprompt"
        timed = "Scenario step\n⎿ $ python3 x (1m 26s)\n(ctrl+b to run in background)\nprompt"
        self.assertEqual(su.screen_digest(base)["sha"], su.screen_digest(timed)["sha"])

    def test_native_part_cuts_at_the_panel_left_border(self):
        self.assertEqual(su.native_part("abc    │ x │"), "abc")
        self.assertEqual(su.native_part("abc    ┌── ┐"), "abc")
        self.assertEqual(su.native_part("plain"), "plain")

    def test_ansi_is_ignored(self):
        self.assertEqual(su.screen_digest("\x1b[31mred\x1b[0m")["sha"], su.screen_digest("red")["sha"])

    def test_tmux_numbers(self):
        self.assertEqual(su.tmux_numbers("12,3\n"), (12, 3))
        self.assertEqual(su.tmux_numbers("x"), (None, None))


class ReleaseCopyTest(unittest.TestCase):
    def make_source(self, root: Path) -> Path:
        source = root / "src"
        shell = source / "utilities" / "session-shell"
        shell.mkdir(parents=True)
        (shell / "host.cjs").write_text("#!/usr/bin/env node\nconsole.log('host');\n", encoding="utf-8")
        (source / "kept.txt").write_text("tracked\n", encoding="utf-8")
        (source / "ignored.log").write_text("ignored\n", encoding="utf-8")
        (source / ".gitignore").write_text("*.log\n", encoding="utf-8")
        git = ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-C", str(source)]
        subprocess.run(["git", "init", "-q", str(source)], check=True)
        subprocess.run(git + ["add", "-A"], check=True)
        subprocess.run(git + ["commit", "-qm", "x"], check=True)
        (source / "untracked.txt").write_text("new\n", encoding="utf-8")
        return source

    def test_copy_carries_working_tree_and_adds_only_the_marker(self):
        with tempfile.TemporaryDirectory() as raw:
            source = self.make_source(Path(raw))
            copy = su.release_copy(source, Path(raw) / "b")
            self.assertTrue((copy / "kept.txt").is_file())
            self.assertTrue((copy / "untracked.txt").is_file())
            self.assertFalse((copy / "ignored.log").exists())
            marker = copy / "utilities" / "session-shell" / su.MARKER_FILE
            self.assertTrue(marker.is_file())
            self.assertFalse((source / "utilities" / "session-shell" / su.MARKER_FILE).exists())
            self.assertNotIn(su.FAIL_ENV, (copy / "utilities/session-shell/host.cjs").read_text())

    def test_fail_copy_throws_only_for_a_successor_and_keeps_the_shebang(self):
        with tempfile.TemporaryDirectory() as raw:
            copy = su.release_copy(self.make_source(Path(raw)), Path(raw) / "b", fail=True)
            text = (copy / "utilities/session-shell/host.cjs").read_text()
            self.assertTrue(text.startswith("#!/usr/bin/env node\n"))
            self.assertIn(su.FAIL_ENV, text)
            self.assertIn("HEARTING_SHELL_GENERATION", text)
            self.assertEqual(su.inject_failure(text), text)   # idempotent
            self.assertTrue(su.inject_failure("x();").startswith("if("))


class ActivationTest(unittest.TestCase):
    def test_activation_is_the_packaged_runtime_activate_of_the_release_copy(self):
        argv = su.activation_argv(Path("/src"), "codex", Path("/fx/release-b"))
        self.assertEqual(argv[:2], ["sh", "/src/tools/install/harness.sh"])
        self.assertEqual(argv[2:], ["runtime", "activate", "--runtime", "codex", "--mode", "packaged",
                                    "--source", "/fx/release-b", "--json"])

    def test_release_copies_differ_only_by_their_marker_and_carry_no_git_dir(self):
        with tempfile.TemporaryDirectory() as raw:
            source = ReleaseCopyTest().make_source(Path(raw))
            one = su.release_copy(source, Path(raw) / "a", label="A")
            two = su.release_copy(source, Path(raw) / "b", label="B")
            marker = Path("utilities/session-shell") / su.MARKER_FILE
            self.assertNotEqual((one / marker).read_text(), (two / marker).read_text())
            self.assertFalse((one / ".git").exists())
            self.assertEqual((one / "kept.txt").read_text(), (two / "kept.txt").read_text())


class BindingTest(unittest.TestCase):
    def test_only_live_bindings_are_read_newest_first(self):
        with tempfile.TemporaryDirectory() as raw:
            env = fixture_env(Path(raw))
            folder = Path(env["XDG_STATE_HOME"]) / "agent-fleet" / "sessions" / "codex"
            folder.mkdir(parents=True)
            alive = {"host_pid": os.getpid(), "native_pid": os.getppid()}
            (folder / "session-shell-1.json").write_text(json.dumps(
                {"host_pid": 2 ** 22 + 5, "native_pid": os.getppid()}), encoding="utf-8")
            (folder / "session-shell-2.json").write_text(json.dumps(alive), encoding="utf-8")
            (folder / "session-shell-3.json").write_text("{not json", encoding="utf-8")
            found = su.bindings(env, "codex")
            self.assertEqual([b["host_pid"] for b in found], [os.getpid()])
            self.assertEqual(su.bindings(env, "claude"), [])


class TableTest(unittest.TestCase):
    ROWS = [{"harness": "claude", "scenario": "session-upgrade", "verdict": "pass",
             "evidence": {"upgrade": {"native_before": 11, "native_after": 11,
                                      "host_before": 20, "host_after": 31,
                                      "screen_same": True, "l3_before": {"history_size": "3"}}}},
            {"harness": "codex", "scenario": "session-upgrade-page", "verdict": "not-produced",
             "evidence": {"upgrade": {"deferred": "not-produced: no route row"}}},
            {"harness": "opencode", "scenario": "route-resume", "verdict": "error",
             "evidence": {}}]

    def test_table_has_a_header_row_per_result_and_dashes_for_blanks(self):
        table = su.render_table(self.ROWS).splitlines()
        self.assertEqual(len(table), 2 + len(self.ROWS))
        self.assertIn("native_before", table[0])
        self.assertIn("| claude | session-upgrade | pass | 11 | 11 | 20 | 31 |", table[2])
        self.assertIn("not-produced: no route row", table[3])
        self.assertIn("| opencode | route-resume | error | - |", table[4])
        self.assertEqual({line.count("|") for line in table}, {table[0].count("|")})

    def test_table_command_reads_a_results_file(self):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "results.json"
            path.write_text(json.dumps(self.ROWS), encoding="utf-8")
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(su.main(["--table", str(path)]), 0)
            self.assertEqual(out.getvalue(), su.render_table(self.ROWS))


class ScriptTest(unittest.TestCase):
    def record(self, text, kind="user", index=0):
        return {"last_text": text, "last_kind": kind, "main_index": index, "text": text}

    def test_upgrade_script_answers_each_state_the_variants_ask_for(self):
        answer = su.SessionUpgradeScenario().script(None)
        self.assertIn("upgrade idle", answer(self.record("SCENARIO-UPGRADE idle"), []).text)
        scroll = answer(self.record("SCENARIO-UPGRADE-SCROLL fill"), [])
        self.assertGreaterEqual(len(scroll.text.splitlines()), 150)
        self.assertIn("native-question", str(answer(self.record("SCENARIO-UPGRADE-ASK x"), []).shell))
        long_turn = answer(self.record("SCENARIO-UPGRADE-LONG run"), [])
        # Claude Code refuses a foreground `sleep N`; the long step must not start with one.
        self.assertIn("long-shell: python3 -c", str(long_turn.shell))
        self.assertNotIn("long-shell: sleep", str(long_turn.shell))
        self.assertEqual(answer(self.record(su.KEY_TEXT), []).text, "key noted")

    def test_route_resume_script_composes_then_commits_and_finishes_only_the_closed_one(self):
        scenario = su.RouteResumeScenario()
        scenario.repos = [Path("/fx/o1"), Path("/fx/o2"), Path("/fx/o3")]
        scenario.state = {"current": 0, "routes": {}}
        scenario.root = Path("/fx")
        run = type("R", (), {})()
        run.__class__.__module__ = "harness_behavior_scenarios"
        answer = scenario.script(run)
        first = answer(self.record("SCENARIO-RR 1 commit"), [])
        self.assertIn("FX-COMMIT-1-OK", first.shell)
        refusal = ("has no route for /fx/o1\n  python3 /x/capability-route.py compose --shape s\n")
        compose = answer(self.record(refusal, "tool"), [])
        self.assertIn("FX-COMPOSE-1-RC", compose.shell)
        scenario_text = 'FX-COMPOSE-1-RC=0 "route_id": "rt-abc12345"'
        commit = answer(self.record(scenario_text, "tool"), [])
        self.assertIn("FX-COMMIT-1-OK", commit.shell)
        self.assertEqual(scenario.state["routes"][1], "rt-abc12345")
        finish = answer(self.record("FX-COMMIT-1-OK", "tool"), [])
        self.assertIn("FX-FINISH-1-RC", finish.shell)
        second = answer(self.record("SCENARIO-RR 2 commit"), [])
        self.assertIn("compose --shape direct --campaign-key other2", second.shell)
        self.assertIn("FX-COMPOSE-2-RC", second.shell)
        answer(self.record('FX-COMPOSE-2-RC=0 "route_id": "rt-def67890"', "tool"), [])
        open_route = answer(self.record("FX-COMMIT-2-OK", "tool"), [])
        self.assertEqual(open_route.text, "route 2 done")


class VerdictTest(unittest.TestCase):
    """The held variants' verdict, driven through ``drive`` with the live plumbing replaced."""

    class Run:
        harness = "claude"

        def __init__(self):
            self.evidence = {}
            self.notes = []
            self.tui = self

        def note(self, text): self.notes.append(text)
        def prompt(self, text): return True
        def wait_requests(self, count, timeout): return True
        def wait_idle(self, quiet, timeout): return True
        def main_requests(self): return []
        def screen(self): return self.text
        text = "native screen\nDRAFT-KEPT-HERE"

    def drive(self, scenario, *, changes, draft_on_screen=True, same_pids=True):
        """``changes``: one answer per ``wait_host_change`` call (after release, then after the next prompt)."""
        answers = iter(changes)
        run = self.Run()
        run.text = "native screen" + ("\nDRAFT-KEPT-HERE" if draft_on_screen else "")
        snap = {"host_pid": 10, "native_pid": 20, "digest": "d", "cursor": (1, 1), "history": "h", "history_size": 0}
        after = dict(snap, host_pid=11 if any(changes) else 10)
        shots = iter([snap, snap, after])
        patches = {
            "snapshot": lambda *a, **k: next(shots),
            "session_id": lambda run: "sid",
            "install_release_b": lambda run, fail=False: {"exit": 0},
            "wait_host_change": lambda *a, **k: next(answers),
            "key_reaches_native": lambda run: True,
            "host_status": lambda run: {},
            "DEFER_WAIT": 0,
        }
        saved = {name: getattr(su, name) for name in patches}
        for name, value in patches.items():
            setattr(su, name, value)
        scenario.hold_state = lambda run: "held"
        scenario.release_state = lambda run: None
        scenario.first_turn = lambda run: True
        try:
            verdict = scenario.drive(run)
        finally:
            for name, value in saved.items():
                setattr(su, name, value)
        return verdict, run.evidence["upgrade"]

    def test_a_draft_switch_right_after_release_passes_and_is_recorded_as_such(self):
        scenario = su.DraftScenario()
        scenario.DRAFT = "DRAFT-KEPT-HERE"
        verdict, ev = self.drive(scenario, changes=[True])
        self.assertEqual(verdict, "pass", ev)
        self.assertIs(ev["switched_after_release"], True)
        self.assertEqual(ev["switched_after_next_prompt"], "-")

    def test_a_draft_switch_only_after_the_next_turn_passes_and_is_recorded_as_such(self):
        scenario = su.DraftScenario()
        scenario.DRAFT = "DRAFT-KEPT-HERE"
        verdict, ev = self.drive(scenario, changes=[None, True])
        self.assertEqual(verdict, "pass", ev)
        self.assertIs(ev["switched_after_release"], False)
        self.assertIs(ev["switched_after_next_prompt"], True)

    def test_no_switch_even_after_the_next_turn_fails(self):
        scenario = su.QuestionScenario()
        verdict, ev = self.drive(scenario, changes=[None, None])
        self.assertEqual(verdict, "fail", ev)
        self.assertIs(ev["switched_after_next_prompt"], False)

    def test_a_lost_draft_byte_fails(self):
        scenario = su.DraftScenario()
        scenario.DRAFT = "DRAFT-KEPT-HERE"
        verdict, ev = self.drive(scenario, changes=[True], draft_on_screen=False)
        self.assertEqual(verdict, "fail", ev)
        self.assertIs(ev["draft_kept"], False)

    def test_other_held_states_still_need_the_switch_right_after_release(self):
        verdict, ev = self.drive(su.TurnScenario(), changes=[None, True])
        self.assertEqual(verdict, "fail", ev)
        self.assertIs(ev["switched_after_next_prompt"], True)


if __name__ == "__main__":
    unittest.main()
