"""A worker's raw tool input and words never reach the public doer-activity surfaces.

PR #1552 review: masking free text with more patterns cannot be made complete, so the
public model carries only allow-listed summaries (a worktree file, an executable and one
listed action of that executable or worktree file). Every case below plants one marker in a
tool call (open
and just finished) and in the worker's own words, and the marker must be absent from all
public places: the job JSON, Fleet's activity rows and a one-step route's folded row, the
shell feed row, and the shell panel painted at 40 and 80 columns and folded at 52.
"""

import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "tools"))

from fleet import session_line, session_panel  # noqa: E402
from fleet.collectors import dispatch as dispatch_collector  # noqa: E402
from fleet.explorer import live, tables  # noqa: E402
from fleet.explorer.grid import CellGrid  # noqa: E402
from fleet.model import DispatchJob  # noqa: E402

MARK = "DEMO1552SensitiveValue"
# A short lowercase value looks like a subcommand word; no shape may admit it (R2 P1).
LOWER_MARK = "qzsecret"
B64 = base64.b64encode(MARK.encode()).decode()


def _shell_cases(mark, b64):
    # The reviewer's thirteen classes, then classes found while fixing, then the R2 six.
    return {
        "base64_body": f"printf {b64} | base64 -d",
        "inline_key": f"FOO_KEY={mark} python task.py",
        "authorization": f'curl -H "Authorization: Bearer {mark}" http://localhost',
        "api_header": f'curl -H "X-Api-Key: {mark}" http://localhost',
        "password_short": f"mysql -u fixture -p {mark}",
        "url_userinfo": f"psql postgresql://fixture:{mark}@localhost/db",
        "url_query": f'curl "https://localhost/?credential={mark}"',
        "heredoc": f"python3 - <<'PY'\nvalue = '{mark}'\nPY",
        "json_password": f"""curl -d '{{"password":"{mark}"}}' http://localhost""",
        "control_token": f"python task.py --token {mark}",
        "token_shaped": "status sk-" + mark,
        "echo_first_word": f"echo {mark}",
        "option_equals": f"tool --password={mark}",
        "export_then": f"export K={mark} && make build",
        "cd_then_wrapped": f"cd /w && bash -lc 'deploy {mark} --now'",
        "pipe_tail": f"cat notes.txt | grep {mark}",
        "second_line": f"set -e\nrun {mark}",
        "here_string": f"sudo -S true <<< {mark}",
        "unbalanced_quote": f"say '{mark}",
        "subshell": f"$(printf {mark})",
        "git_url": f"git clone https://fixture:{mark}@localhost/repo.git",
        "missing_file_arg": f"python3 {mark}.py",
        "path_outside_tree": f"python3 /etc/{mark}/run.py",
        "plain_first_word": f"mysql {mark}",
        "ssh_first_word": f"ssh {mark}",
        "quoted_first_word": f"mysql '{mark}'",
        "env_assignment_first_word": f"env AUTH=OMITTED mysql {mark}",
        "sudo_first_word": f"sudo mysql {mark}",
        "cd_wrapped_first_word": f"cd x && bash -lc 'mysql {mark}'",
        "unlisted_action_shape": f"git {mark}",
    }


SHELL = _shell_cases(MARK, B64)
FILE_TOOLS = {
    "edit_body": ("Edit", {"file_path": "src/app.py", "old_string": MARK, "new_string": MARK}),
    "write_body": ("Write", {"file_path": "src/new.py", "content": MARK}),
    "read_outside_dir": ("Read", {"file_path": f"/srv/{MARK}/id_key"}),
    "grep_pattern": ("Grep", {"pattern": MARK, "path": "."}),
    "web_url": ("WebFetch", {"url": f"https://localhost/?k={MARK}"}),
    "mcp_input": ("mcp__notes__save", {"text": MARK}),
}
WORDS = (f'The password is "{MARK}".', f"설정의 비밀번호는 {MARK} 입니다.",
         f"API_KEY={MARK} 로 다시 시도한다")


class DoerActivityPublicBoundaryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="pr1552-boundary-")
        root = self.tmp.name
        self.tree = os.path.join(root, "tree")
        os.makedirs(os.path.join(self.tree, "src"))
        Path(self.tree, "notes.txt").write_text("")
        os.makedirs(os.path.join(self.tree, "tools"))
        Path(self.tree, "tools", "run-tests.py").write_text("")
        self.logs = os.path.join(root, ".dispatch", "logs")
        os.makedirs(self.logs)
        self.env = mock.patch.dict(os.environ, {
            "AGENT_HOME": root, "CLAUDE_CONFIG_DIR": root,
            "XDG_CACHE_HOME": os.path.join(root, "cache"),
            "XDG_STATE_HOME": os.path.join(root, "state")})
        self.env.start()
        dispatch_collector._CLAUDE_STREAM_CACHE.clear()
        dispatch_collector._CODEX_ATTEMPT_CACHE.clear()

    def tearDown(self):
        dispatch_collector._CLAUDE_STREAM_CACHE.clear()
        dispatch_collector._CODEX_ATTEMPT_CACHE.clear()
        self.env.stop()
        self.tmp.cleanup()

    def _claude_job(self, index, name, tool_input, mark=MARK):
        attempt = "att-boundary-%d" % index
        job = DispatchJob(key="k%d" % index, slug="owner", pid=91, proc_start="wrapper",
                          harness="claude", attempt_id=attempt, is_child=True,
                          liveness="working", cwd=self.tree)
        job._log_file = os.path.join(self.logs, "owner.%s.claude.jsonl" % attempt)
        sid = "sid-%d" % index
        usage = {"input_tokens": 10, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}
        rows = [
            {"type": "system", "subtype": "init", "session_id": sid, "model": "claude-fable-5",
             "claude_code_version": "2.1.299"},
            {"type": "assistant", "session_id": sid, "message": {
                "model": "claude-fable-5", "usage": usage, "content": [
                    {"type": "text", "text": WORDS[index % len(WORDS)]},
                    {"type": "tool_use", "id": "done", "name": name, "input": tool_input}]}},
            {"type": "user", "session_id": sid, "message": {"content": [
                {"type": "tool_result", "tool_use_id": "done", "content": mark}]}},
            {"type": "assistant", "session_id": sid, "timestamp": "2026-11-12T01:00:00Z",
             "message": {"model": "claude-fable-5", "usage": usage, "content": [
                 {"type": "text", "text": WORDS[(index + 1) % len(WORDS)]},
                 {"type": "tool_use", "id": "open", "name": name, "input": tool_input}]}},
        ]
        Path(job._log_file).write_text("".join(json.dumps(row) + "\n" for row in rows))
        dispatch_collector._enrich_claude_stream_session(job, fast_first=True)
        return job

    def _codex_job(self, index, command, mark=MARK):
        attempt = "att-codex-boundary-%d" % index
        job = DispatchJob(key="c%d" % index, slug="owner", harness="codex", attempt_id=attempt,
                          liveness="working", cwd=self.tree)
        path = os.path.join(self.logs, "owner.%s.codex.jsonl" % attempt)
        rows = [{"type": "dispatch.supervisor.turn.started", "thread_id": "t", "turn_id": "u"},
                {"type": "item.started", "item": {"type": "command_execution", "id": "a",
                                                  "command": command}},
                {"type": "item.completed", "item": {"type": "command_execution", "id": "a",
                                                    "aggregated_output": mark}},
                {"type": "item.completed", "item": {"type": "agent_message", "text": WORDS[0]}},
                {"type": "item.started", "item": {"type": "command_execution", "id": "b",
                                                  "command": command}}]
        Path(path).write_text("".join(json.dumps(row) + "\n" for row in rows))
        parsed = dispatch_collector._parse_codex_attempt_tail(path)
        dispatch_collector._job_activity(job, parsed.get("exec_tool"), parsed.get("last_tool"))
        return job

    def _surfaces(self, job):
        doer = live._route_doer(None, job, False, now=1794445400.0)
        fleet = "\n".join("".join(part[0] for part in row)
                          for row in tables.route_activity_rows({"doer": doer}))
        # The folded ROUTES row of a one-step route carries the same summary (2026-11-12).
        folded = {"id": "r", "title": "t", "age": "1m", "state": "working", "activity": "running",
                  "harness": "claude", "legs": [["code", "active", None]], "tree_label": "code",
                  "stages": [{"name": "one-shot", "st": "active"}], "doer": doer}
        for width in (120, 150):
            grid = CellGrid(width, 1)
            tables.draw_route_row(grid, 0, width, 0, folded,
                                  tables.route_row_columns(width, (folded,)))
            fleet += "\n" + "".join(str(cell[0] or "") for cell in grid.cells[0])
        shell = session_line.route_doer({"doer": doer}, [], lambda *a, **k: None, live)
        feed = session_panel.route_row({"id": "r", "state": "working", **shell})
        route = {"id": "r", "title": "Synthetic task", "state": "working", "kind": "working",
                 **shell, "paint": feed}
        return job.to_dict(), fleet, {"shell": shell, "feed": feed}, route

    def test_no_marker_reaches_any_public_surface(self):
        jobs = {}
        for index, (case, command) in enumerate(SHELL.items()):
            jobs["claude:" + case] = self._claude_job(index, "Bash", {"command": command})
            jobs["codex:" + case] = self._codex_job(index, command)
            wrapped = "/bin/bash -lc " + json.dumps(command)
            jobs["codex-wrapped:" + case] = self._codex_job(1000 + index, wrapped)
        for index, (case, (name, tool_input)) in enumerate(FILE_TOOLS.items(), start=500):
            jobs["claude:" + case] = self._claude_job(index, name, tool_input)
        self.assertEqual(self._leaks(jobs, MARK, B64), [])
        # The surfaces were really drawn: the allow-listed summaries are there.
        self.assertEqual(jobs["claude:edit_body"].exec_tool["target"], "src/app.py")
        self.assertEqual(jobs["claude:edit_body"].last_tool, {"name": "Edit", "target": "src/app.py"})
        self.assertEqual(jobs["claude:pipe_tail"].exec_tool, {"name": "cat", "target": "notes.txt",
                                                              "since": 1794445200.0})
        self.assertEqual(jobs["codex-wrapped:export_then"].exec_tool, {"name": "make"})
        painted = self._painted
        self.assertIn("after Edit · src/app.py", painted["claude:edit_body"]["40"].replace("\n", ""))
        self.assertIn(">_ cat · notes.txt", painted["claude:pipe_tail"]["80"])
        # The folded places were really drawn too.
        _public, fleet, _feed, _route = self._surfaces(jobs["claude:edit_body"])
        self.assertIn("● Edit src/app.py", fleet)
        self.assertIn(">_ cat", painted["claude:pipe_tail"]["52-folded"])

    def test_short_lowercase_marker_reaches_no_public_surface(self):
        lower_b64 = base64.b64encode(LOWER_MARK.encode()).decode()
        jobs = {}
        for index, (case, command) in enumerate(_shell_cases(LOWER_MARK, lower_b64).items()):
            jobs["claude:" + case] = self._claude_job(
                index, "Bash", {"command": command}, mark=LOWER_MARK)
            jobs["codex:" + case] = self._codex_job(index, command, mark=LOWER_MARK)
        positives = {"git push": {"name": "git", "target": "push"},
                     "cat notes.txt": {"name": "cat", "target": "notes.txt"},
                     "python3 tools/run-tests.py": {"name": "python3",
                                                    "target": "tools/run-tests.py"}}
        for index, command in enumerate(positives, start=800):
            jobs["positive:" + command] = self._claude_job(index, "Bash", {"command": command},
                                                           mark=LOWER_MARK)
        self.assertEqual(self._leaks(jobs, LOWER_MARK, lower_b64), [])
        self.assertEqual(jobs["claude:plain_first_word"].exec_tool["name"], "mysql")
        self.assertNotIn("target", jobs["claude:plain_first_word"].exec_tool)
        self.assertEqual(jobs["claude:sudo_first_word"].last_tool, {"name": "mysql"})
        for command, expected in positives.items():
            job = jobs["positive:" + command]
            self.assertEqual({k: job.exec_tool[k] for k in ("name", "target")}, expected)
            self.assertEqual(job.last_tool, expected)

    def _leaks(self, jobs, mark, b64):
        routes, leaks = {}, []
        for case, job in jobs.items():
            public, fleet, feed, route = self._surfaces(job)
            routes[case] = route
            for place, value in (("job_json", json.dumps(public, ensure_ascii=False)),
                                 ("fleet_rows", fleet),
                                 ("shell_feed", json.dumps(feed, ensure_ascii=False))):
                if mark in value or b64 in value:
                    leaks.append((case, place, value))
        painted = self._painted = self._paint(routes)
        for case, widths in painted.items():
            for width, text in widths.items():
                if mark in text or b64 in text:
                    leaks.append((case, "panel_%s" % width, text))
        return leaks

    def _paint(self, routes):
        node = shutil.which("node")
        if node is None:
            self.skipTest("node is not available")
        source = Path(self.tmp.name, "routes.json")
        source.write_text(json.dumps(routes, ensure_ascii=False))
        script = r"""
const fs=require('fs'),view=require(process.argv[1]),routes=JSON.parse(fs.readFileSync(process.argv[2],'utf8'));
const out={};for(const [key,route] of Object.entries(routes)){out[key]={};for(const width of [40,80,'52-folded']){
 const w=parseInt(width),folded=String(width).endsWith('folded');
 const x=view.render({width:w,height:40,side:folded||w<80,model:{routes:[route],route_count:1,messages:[]},sid:'s',
  configuration:{},expandedKeys:folded?[]:['route:r'],questions:()=>[]});
 out[key][width]=x.rows.map(r=>r.map(p=>p.text).join('')).join('\n');}}
process.stdout.write(JSON.stringify(out));"""
        result = subprocess.run(
            [node, "-e", script, str(ROOT / "utilities/session-shell/panel-view.cjs"), str(source)],
            capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)


class ToolStateNeedsCompleteObservationTest(unittest.TestCase):
    """R2 P2: an open call and "no tool" are settled only from what was read without a gap;
    an empty log or an unread middle that may hold the deciding rows draws neither."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="pr1552-gap-")
        self.tree = os.path.join(self.tmp.name, "tree")
        os.makedirs(self.tree)
        self.env = mock.patch.dict(os.environ, {
            "AGENT_HOME": self.tmp.name, "CLAUDE_CONFIG_DIR": self.tmp.name,
            "XDG_CACHE_HOME": os.path.join(self.tmp.name, "cache"),
            "XDG_STATE_HOME": os.path.join(self.tmp.name, "state")})
        self.env.start()
        dispatch_collector._CLAUDE_STREAM_CACHE.clear()
        dispatch_collector._CODEX_ATTEMPT_CACHE.clear()
        self.count = 0

    def tearDown(self):
        dispatch_collector._CLAUDE_STREAM_CACHE.clear()
        dispatch_collector._CODEX_ATTEMPT_CACHE.clear()
        self.env.stop()
        self.tmp.cleanup()

    SID = "sid-gap"

    def _assistant(self, content):
        return {"type": "assistant", "session_id": self.SID,
                "message": {"model": "claude-fable-5", "usage": {"input_tokens": 10},
                            "content": content}}

    def _use(self, ident, command):
        return self._assistant([{"type": "tool_use", "id": ident, "name": "Bash",
                                 "input": {"command": command}}])

    def _result(self, ident):
        return {"type": "user", "session_id": self.SID, "message": {"content": [
            {"type": "tool_result", "tool_use_id": ident, "content": "done"}]}}

    def _pad(self, size):
        return self._assistant([{"type": "text", "text": "x" * size}])

    def _claude(self, rows):
        self.count += 1
        attempt = "att-gap-%d" % self.count
        job = DispatchJob(key=attempt, slug="owner", pid=91, proc_start="wrapper",
                          harness="claude", attempt_id=attempt, is_child=True,
                          liveness="working", cwd=self.tree)
        logs = os.path.join(self.tmp.name, ".dispatch", "logs")
        os.makedirs(logs, exist_ok=True)
        job._log_file = os.path.join(logs, "owner.%s.claude.jsonl" % attempt)
        Path(job._log_file).write_text("".join(json.dumps(row) + "\n" for row in rows))
        dispatch_collector._enrich_claude_stream_session(job, fast_first=True)
        doer = live._route_doer(None, job, False, now=1794445400.0)
        return job, doer

    def test_claude_gap_and_empty_log_draw_no_tool_state(self):
        init = {"type": "system", "subtype": "init", "session_id": self.SID,
                "model": "claude-fable-5"}
        cases = {
            # The open call sits only in the unread middle.
            "hidden_open": [init, self._pad(100000), self._use("long", "sleep 9"),
                            self._pad(620000), self._assistant([{"type": "text", "text": "."}])],
            # A head call closed in the unread middle.
            "hidden_close": [init, self._use("long", "sleep 9"), self._pad(100000),
                             self._result("long"), self._pad(620000),
                             self._assistant([{"type": "text", "text": "."}])],
            "empty_log": [],
        }
        for case, rows in cases.items():
            job, doer = self._claude(rows)
            self.assertIsNone(job.exec_tool, case)
            self.assertFalse(job._tool_observed, case)
            self.assertNotIn("idle_tool", doer, case)
            self.assertIsNone(doer.get("tool"), case)

    def test_claude_tail_settles_what_it_read_in_full(self):
        init = {"type": "system", "subtype": "init", "session_id": self.SID,
                "model": "claude-fable-5"}
        # A call opened in the tail is open: its close could only come later.
        job, doer = self._claude([init, self._use("old", "sleep 1"), self._pad(700000),
                                  self._use("now", "sleep 9")])
        self.assertEqual(doer.get("tool"), "sleep")
        # A model row after a tool result in the tail: every earlier call had returned.
        job, doer = self._claude([init, self._use("old", "sleep 1"), self._pad(700000),
                                  self._use("t", "git push"), self._result("t"),
                                  self._assistant([{"type": "text", "text": "."}])])
        self.assertIsNone(job.exec_tool)
        self.assertTrue(doer.get("idle_tool"))
        self.assertEqual(job.last_tool, {"name": "git", "target": "push"})
        # A turn end read in the tail settles absence too.
        job, doer = self._claude([init, self._use("old", "sleep 1"), self._pad(700000),
                                  {"type": "result", "session_id": self.SID}])
        self.assertTrue(doer.get("idle_tool"))
        self.assertIsNone(job.last_tool)   # the head's close may be superseded in the gap
        # Head and tail meet without a gap: the row across the 64KiB seam is still read.
        job, doer = self._claude([init, self._pad(65000), self._use("seam", "sleep 9"),
                                  self._pad(70000)])
        self.assertEqual(doer.get("tool"), "sleep")
        self.assertTrue(job._tool_observed)

    def test_codex_gap_settles_no_command_only_after_a_turn_boundary(self):
        path = os.path.join(self.tmp.name, "owner.att-codex-gap.codex.jsonl")

        def parse(rows):
            Path(path).write_text("".join(json.dumps(row) + "\n" for row in rows))
            dispatch_collector._CODEX_ATTEMPT_CACHE.clear()
            return dispatch_collector._parse_codex_attempt_tail(path)

        pad = {"type": "item.completed", "item": {"type": "agent_message", "text": "x" * 300000}}
        start = {"type": "dispatch.supervisor.turn.started", "thread_id": "t", "turn_id": "u"}
        hidden = {"type": "item.started", "item": {"type": "command_execution", "id": "a",
                                                   "command": "sleep 9"}}
        parsed = parse([{"type": "thread.started", "thread_id": "t"}, start, pad, hidden,
                        pad, pad])
        self.assertIsNone(parsed["exec_tool"])
        self.assertFalse(parsed["tool_observed"])
        parsed = parse([{"type": "thread.started", "thread_id": "t"}, start, pad, hidden,
                        pad, pad, dict(start, turn_id="v")])
        self.assertTrue(parsed["tool_observed"])
        self.assertTrue(parse([start])["tool_observed"])
        self.assertFalse(parse([])["tool_observed"])


if __name__ == "__main__":
    unittest.main()
