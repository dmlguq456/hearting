"""Under Claude's input inside the shell: one statusline row, and a notice line with only
what the person must do now (2026-11-14). What the notice line no longer carries stays
on screen: received news in MESSAGES, a route's result in the ROUTE box and a waiting
release in the panel's tidy-notice slot."""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import json
from pathlib import Path
import re
import subprocess
import tempfile
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "utilities"))
sys.path.insert(0, str(ROOT / "tools"))
import session_shell as shell  # noqa: E402
import human_wait_state  # noqa: E402
import session_tidy  # noqa: E402
from fleet import session_line, session_panel, session_tags  # noqa: E402
from fleet.config import ui_text  # noqa: E402
from fleet.explorer.grid import cell_width  # noqa: E402

ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
SID = "00000000-0000-4000-8000-000000001114"


def plain(parts):
    return "".join(text for text, _key in parts)


class ClaudeStatuslineRowTest(unittest.TestCase):
    def native_row(self, columns, jobs=""):
        """Claude's callback with values long enough to wrap (measured 11-14 before the
        change: two rows at 113 columns, three at 80 and 60, one more with a running job)."""
        now = time.time()
        callback = {"session_id": "synthetic-input-rows", "cwd": "/synthetic/hearting",
                    "model": {"display_name": "Opus 5.5"}, "effort": {"level": "xhigh"},
                    "context_window": {"used_percentage": 58},
                    "rate_limits": {"five_hour": {"used_percentage": 12, "resets_at": now + 7200},
                                    "seven_day": {"used_percentage": 66, "resets_at": now + 5 * 86400}}}
        with tempfile.TemporaryDirectory(prefix="pir-") as directory:
            private = Path(directory) / "shell"
            private.mkdir(mode=0o700)
            env = {"HEARTING_SHELL_SOCKET": str(private / "control.sock"), "COLUMNS": str(columns),
                   "TERM": "xterm-256color", "S_SESSION_DISPLAY": "Panel input rows",
                   "git_repo": "1", "git_branch": "panel-input-rows-1114", "git_operation": "none",
                   "git_merged": "0", "git_branch_done": "0", "git_extra_worktrees": "12",
                   "git_ahead": "2", "git_behind": "3", "git_dirty": "1", "git_jobs": jobs}
            with mock.patch.object(human_wait_state, "cached_statusline", return_value=((1, 1), "route 3 ⠏2 ✕1")), \
                    mock.patch.object(session_tags, "assigned_tag", return_value="[e9]"):
                painted = session_line.claude_native_statusline(callback, env)
            record = json.loads((private / "claude-status.json").read_text())
        return ANSI.sub("", painted), record

    def test_inside_the_shell_the_statusline_is_one_row_at_every_width(self):
        for columns in (113, 80, 60):
            for jobs in ("", "pytest fleet"):
                with self.subTest(columns=columns, jobs=jobs):
                    line, record = self.native_row(columns, jobs)
                    self.assertNotIn("\n", line, "one row under the input box")
                    self.assertLessEqual(cell_width(line), columns)
                    # The shell blanks exactly this one recorded row.
                    self.assertEqual(record["lines"], [line])
                    # The action counts stay at every width (line_segments' own narrow rule).
                    self.assertIn("route 3", line)
                    self.assertRegex(line, r"◑(답 대기| to answer)? 1")
                    if columns >= 80:
                        for field in ("58%", "5h 12%", "7d 66%"):
                            self.assertIn(field, line)
        # A dropped group returns its cells: the model keeps its name where it fits.
        self.assertIn("Opus 5.5 xhigh", self.native_row(113, "pytest fleet")[0])
        # Where the row has room, a running job ends it (the panel paints it while expanded).
        self.assertIn(">_ pytest fleet", self.native_row(200, "pytest fleet")[0])

    def test_the_wrapped_callback_through_statusline_sh_paints_one_row(self):
        # The real callback script, as Claude runs it inside the shell, at 60 columns.
        with tempfile.TemporaryDirectory(prefix="pir-sh-") as directory:
            root = Path(directory)
            (root / "claude").mkdir()
            env = dict(os.environ, HOME=str(root), CLAUDE_CONFIG_DIR=str(root / "claude"), AGENT_HOME=str(root / "no-code"),
                       XDG_STATE_HOME=str(root / "state"), FLEET_TITLE_DISABLE="1", HARNESS_CAPACITY_REFRESH_DISABLE="1",
                       COLUMNS="60")
            payload = {"session_id": "synthetic-input-rows", "cwd": str(root),
                       "model": {"id": "claude-opus", "display_name": "Opus"},
                       "context_window": {"used_percentage": 37},
                       "rate_limits": {"five_hour": {"used_percentage": 12}, "seven_day": {"used_percentage": 42}}}
            outputs = {}
            for wrapped in (True, False):
                run = dict(env)
                run.pop("HEARTING_SHELL_SOCKET", None)
                if wrapped:
                    run["HEARTING_SHELL_SOCKET"] = str(root / "private.sock")
                result = subprocess.run(["bash", str(ROOT / "adapters/claude/statusline.sh")], input=json.dumps(payload),
                                        env=run, text=True, capture_output=True, timeout=20)
                self.assertEqual(result.returncode, 0, result.stderr)
                outputs[wrapped] = ANSI.sub("", result.stdout).rstrip("\n")
        self.assertEqual(len(outputs[True].split("\n")), 1, outputs[True])
        self.assertLessEqual(cell_width(outputs[True]), 60)
        # Outside the shell the existing bash rendering stays: the whole words, unshortened.
        self.assertIn("context ━━━━────── 37%", outputs[False])
        self.assertIn("5h 12% 7d 42%", outputs[False])


class NarrowLineKeepsTheUrgentTest(unittest.TestCase):
    """The common line's last narrow-width step (Claude's one row, the folded panel strip,
    Fleet's line, Codex and OpenCode): what the person must act on stays, urgent first."""

    def values(self, *, urgent=True, wait=0):
        now = time.time()
        pressure = {"state": "observed", "usage_at": now, "observed_at": now, "fresh_until": now + 600,
                    "scope_tokens": 190000, "limit_tokens": 200000, "scope": "Total", "token_limit_reached": True}
        values = {"tag": "[e9]", "cwd": "/synthetic/hearting", "model": "Opus 5.5", "effort": "xhigh",
                  "branch": "main", "git": {"repo": 1, "operation": "none", "ahead": 0, "behind": 0,
                                            "extra_worktrees": 3},
                  "route_label": "route 2 ⠏1", "answer_wait": wait}
        if urgent:
            values.update(ctx_pct=96, compaction_pressure=pressure,
                          quota_windows=[("5h", 97, now + 3 * 3600), ("7d", 91, now + 3 * 86400)])
        else:
            values.update(ctx_pct=40, quota_windows=[("5h", 30, now + 7200), ("7d", 50, now + 5 * 86400)])
        return values

    def line(self, values, columns):
        text = plain(session_line.line_segments(values, columns))
        self.assertLessEqual(cell_width(text), columns)
        return text

    def test_compaction_and_red_readings_outlast_the_route_count(self):
        imminent = ui_text("fleet.context.compaction_imminent")
        for wait in (0, 1):
            for columns in (80, 70, 60, 50, 40):
                with self.subTest(wait=wait, columns=columns):
                    text = self.line(self.values(wait=wait), columns)
                    kept = [bool(wait) and re.search(r"◑(답 대기| to answer)? 1", text) is not None,
                            imminent in text, "96%" in text, "5h 97%" in text, "7d 91%" in text,
                            "route 2" in text]
                    # Measured before the change: `route 2 ⠏1` alone at 70-40 (with a wait from 80).
                    self.assertTrue(kept[1] and kept[2], text)
                    if wait:
                        self.assertTrue(kept[0], "the answer wait stays: " + text)
                    # Nothing less urgent stays while something more urgent went.
                    self.assertEqual(kept[1:], sorted(kept[1:], reverse=True), text)
                    if columns == 80:
                        self.assertTrue(all(kept[1:]), text)
        # The answer wait is the last to go.
        self.assertEqual(self.line(self.values(wait=1), 12), "◑ 1")

    def test_with_nothing_urgent_the_narrow_line_is_unchanged(self):
        for columns in (60, 50, 40):
            with self.subTest(columns=columns):
                self.assertEqual(self.line(self.values(urgent=False), columns), "route 2 ⠏1")
                self.assertEqual(self.line(self.values(urgent=False, wait=1), columns), "◑ 1 route 2 ⠏1")

    def test_running_jobs_go_before_the_folder_shortens(self):
        values = {**self.values(urgent=False), "jobs": "oncall-watcher pytest"}
        text = plain(session_line.line_segments(values, 120, jobs=True))
        self.assertIn("[e9] hearting/ (main", text)
        self.assertNotIn(">_", text)
        self.assertIn(">_ oncall-watcher pytest", plain(session_line.line_segments(values, 200, jobs=True)))


class NoticeLineTest(unittest.TestCase):
    def setUp(self):
        patch = mock.patch.object(session_panel, "compaction_imminent", side_effect=lambda pressure: pressure == "imminent")
        patch.start()
        self.addCleanup(patch.stop)

    def notice(self, values, routes=(), questions=()):
        return session_panel.notice_line(values, list(routes), list(questions))

    def test_only_what_the_person_must_do_now_by_priority(self):
        calm = {"ctx_pct": 40, "quota_windows": [("5h", 30, None), ("7d", 50, None)]}
        ask = [{"answerable": True, "request_state": "open"}]
        failed = [{"state": "failed", "state_label": "Failed", "title": "shell"}]
        blocked = [{"state": "blocked", "state_label": "?", "title": "review approval"}]
        imminent = {**calm, "compaction_pressure": "imminent"}
        red = {**calm, "ctx_pct": 95}
        usage = {**calm, "quota_windows": [("5h", 85, None)]}
        self.assertEqual(self.notice(calm), [], "nothing to do: an empty line")
        # An answer this session owes comes first.
        self.assertIn(session_line.answer_wait_word() + " 1", plain(self.notice({**imminent, "ctx_pct": 95}, failed, ask)))
        # Then compaction imminent, before a red reading and a route.
        self.assertIn(ui_text("fleet.context.compaction_imminent"), plain(self.notice({**imminent, "ctx_pct": 95}, failed)))
        # Then a red context or usage reading, before a route.
        self.assertIn("95%", plain(self.notice(red, failed)))
        self.assertIn("5h 85%", plain(self.notice(usage, failed)))
        # Then a failed, attention-needing or blocked route.
        self.assertIn("Failed · shell", plain(self.notice(calm, failed)))
        self.assertEqual(plain(self.notice(calm, blocked)), "◑ " + ui_text("fleet.session_block.open") + " · review approval")
        self.assertEqual(self.notice(calm, [{**blocked[0], "state": "working"}]), [], "a working route is calm")

    def test_news_and_release_lines_never_stand_on_it(self):
        calm = {"ctx_pct": 40}
        done = [{"state": "done", "outcome": "PASS", "title": "shell", "result_at": time.time() - 10}]
        self.assertEqual(self.notice(calm, done), [], "a route result is in the ROUTE box")


class RemovedNoticeKindsStayInThePanelTest(unittest.TestCase):
    def project(self, *, routes, label, upgrade_note="", news=True, human_ago=300):
        now = time.time()
        display = {"human": {"at": now - human_ago, "text": "earlier input", "sequence": 1, "source": "real"},
                   "human_state": "real",
                   "received": [{"at": now - 60, "sender": "[e7]", "text": "#1445 merge ready", "sequence": 1,
                                 "harness": "claude", "sid": SID, "source": "peer"}] if news else []}
        view = {"route_panels": {"claude\0" + SID: routes}, "items": [], "by_session": {}}
        with mock.patch.object(session_tidy, "prompt_display", return_value=display), \
                mock.patch.object(human_wait_state, "cached_view", return_value=view), \
                mock.patch.object(human_wait_state, "cached_statusline", return_value=((0, 0), label)):
            return shell.panel_projection("claude", SID, columns=40, rows=40, upgrade_note=upgrade_note)["panel"]

    def render(self, panel, width, height):
        script = r"""
const view=require(process.argv[1]),model=JSON.parse(process.argv[2]),[width,height]=JSON.parse(process.argv[3]);
const v=view.render({width,height,side:width<80,fill:width<80,configuration:{},model,questions:()=>[]});
process.stdout.write(v.rows.map(r=>r.map(s=>s.text).join('')).join('\n'));
"""
        result = subprocess.run(["node", "-e", script, str(ROOT / "utilities/session-shell/panel-view.cjs"),
                                 json.dumps(panel, ensure_ascii=False), json.dumps([width, height])],
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def routes(self):
        now = time.time()
        working = {"id": "rt-working", "title": "Working route", "state": "working", "stages": []}
        finished = {"id": "rt-finished", "title": "Finished route", "state": "done", "outcome": "PASS",
                    "result_at": now - 10, "stages": []}
        return [working, finished]

    def test_news_leaves_the_notice_line(self):
        panel = self.project(routes=self.routes(), label="route 1")
        self.assertEqual(panel["notice"], [], "news is not something to do")

    def test_news_stays_in_messages_and_the_route_box(self):
        panel = self.project(routes=self.routes(), label="route 1")
        for width, height in ((40, 40), (80, 30)):
            with self.subTest(width=width):
                text = self.render(panel, width, height)
                self.assertIn("#1445 merge ready", text, "the received report stays in MESSAGES")
                self.assertIn("Working route", text)
                self.assertIn("Finished route", text, "the result stays in the ROUTE box beside a current route")

    def results(self, ago):
        now = time.time()
        working = {"id": "rt-working", "title": "Working route", "state": "working", "marker": "working", "stages": []}
        passed = {"id": "rt-pass", "title": "Passed route", "state": "done", "marker": "done", "outcome": "success",
                  "result_at": now - ago, "stages": []}
        failed = {"id": "rt-fail", "title": "Failed route", "state": "done", "marker": "done", "outcome": "FAIL",
                  "result_at": now - ago - 5, "stages": []}
        return [working, passed, failed]

    def test_a_result_that_did_not_pass_takes_the_failure_mark_and_its_word(self):
        panel = self.project(routes=self.results(60), label="route 1", news=False)
        shown = {row["id"]: row for row in panel["routes"]}
        self.assertEqual((shown["rt-pass"]["glyph"], shown["rt-pass"]["outcome_key"]), ("✓", "lvl_g"))
        self.assertEqual((shown["rt-fail"]["glyph"], shown["rt-fail"]["outcome_key"]), ("✕", "g_dead"))
        for width, height in ((40, 40), (100, 30)):
            with self.subTest(width=width):
                text = self.render(panel, width, height)
                self.assertIn("✓ success · Passed route", text)
                self.assertIn("✕ FAIL · Failed route", text, "not `✓`, and the folded row keeps the word")

    def test_a_result_leaves_the_route_box_after_the_next_input_or_ten_minutes(self):
        for name, ago, human_ago in (("ten minutes", 11 * 60, 3600), ("next input", 60, 30)):
            with self.subTest(name):
                panel = self.project(routes=self.results(ago), label="route 1", news=False, human_ago=human_ago)
                self.assertEqual([row["id"] for row in panel["routes"]], ["rt-working"])
                text = self.render(panel, 40, 40)
                self.assertIn("Working route", text)
                self.assertNotIn("Passed route", text)
                self.assertNotIn("Failed route", text)
        # With no current route, the box shows the fresh results alone.
        panel = self.project(routes=self.results(60)[1:], label="", news=False)
        self.assertEqual([row["id"] for row in panel["routes"]], ["rt-pass", "rt-fail"])

    def test_release_lines_leave_the_notice_line_and_stay_in_the_tidy_slot(self):
        for note in ("pending", "scrollback"):
            with self.subTest(note=note):
                panel = self.project(routes=[], label="", upgrade_note=note, news=False)
                self.assertEqual(panel["notice"], [], note)
                self.assertNotIn("notice_upgrade", panel)
                words = ui_text("session_shell.upgrade_" + note)
                self.assertEqual(panel["tidy_notice"], words)
                for width, height in ((40, 40), (80, 30)):
                    self.assertIn(words, self.render(panel, width, height), (note, width))


if __name__ == "__main__":
    unittest.main()
