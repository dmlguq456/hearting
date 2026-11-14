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
    def project(self, *, routes, label, upgrade_note="", news=True):
        now = time.time()
        display = {"human": {"at": now - 300, "text": "earlier input", "sequence": 1, "source": "real"},
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
