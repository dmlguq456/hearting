"""A worker's raw tool input and words never reach the public doer-activity surfaces.

PR #1552 review: masking free text with more patterns cannot be made complete, so the
public model carries only allow-listed summaries (a worktree file, an executable and one
subcommand word or worktree file). Every case below plants one marker in a tool call (open
and just finished) and in the worker's own words, and the marker must be absent from all
five public places: the job JSON, Fleet's activity rows, the shell feed row, and the shell
panel painted at 40 and 80 columns.
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
from fleet.model import DispatchJob  # noqa: E402

MARK = "DEMO1552SensitiveValue"
B64 = base64.b64encode(MARK.encode()).decode()

# The reviewer's thirteen classes, then classes found while fixing.
SHELL = {
    "base64_body": f"printf {B64} | base64 -d",
    "inline_key": f"FOO_KEY={MARK} python task.py",
    "authorization": f'curl -H "Authorization: Bearer {MARK}" http://localhost',
    "api_header": f'curl -H "X-Api-Key: {MARK}" http://localhost',
    "password_short": f"mysql -u fixture -p {MARK}",
    "url_userinfo": f"psql postgresql://fixture:{MARK}@localhost/db",
    "url_query": f'curl "https://localhost/?credential={MARK}"',
    "heredoc": f"python3 - <<'PY'\nvalue = '{MARK}'\nPY",
    "json_password": f"""curl -d '{{"password":"{MARK}"}}' http://localhost""",
    "control_token": f"python task.py --token {MARK}",
    "token_shaped": "status sk-" + MARK,
    "echo_first_word": f"echo {MARK}",
    "option_equals": f"tool --password={MARK}",
    "export_then": f"export K={MARK} && make build",
    "cd_then_wrapped": f"cd /w && bash -lc 'deploy {MARK} --now'",
    "pipe_tail": f"cat notes.txt | grep {MARK}",
    "second_line": f"set -e\nrun {MARK}",
    "here_string": f"sudo -S true <<< {MARK}",
    "unbalanced_quote": f"say '{MARK}",
    "subshell": f"$(printf {MARK})",
    "git_url": f"git clone https://fixture:{MARK}@localhost/repo.git",
    "missing_file_arg": f"python3 {MARK}.py",
    "path_outside_tree": f"python3 /etc/{MARK}/run.py",
}
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

    def _claude_job(self, index, name, tool_input):
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
                {"type": "tool_result", "tool_use_id": "done", "content": MARK}]}},
            {"type": "assistant", "session_id": sid, "timestamp": "2026-11-12T01:00:00Z",
             "message": {"model": "claude-fable-5", "usage": usage, "content": [
                 {"type": "text", "text": WORDS[(index + 1) % len(WORDS)]},
                 {"type": "tool_use", "id": "open", "name": name, "input": tool_input}]}},
        ]
        Path(job._log_file).write_text("".join(json.dumps(row) + "\n" for row in rows))
        dispatch_collector._enrich_claude_stream_session(job, fast_first=True)
        return job

    def _codex_job(self, index, command):
        attempt = "att-codex-boundary-%d" % index
        job = DispatchJob(key="c%d" % index, slug="owner", harness="codex", attempt_id=attempt,
                          liveness="working", cwd=self.tree)
        path = os.path.join(self.logs, "owner.%s.codex.jsonl" % attempt)
        rows = [{"type": "dispatch.supervisor.turn.started", "thread_id": "t", "turn_id": "u"},
                {"type": "item.started", "item": {"type": "command_execution", "id": "a",
                                                  "command": command}},
                {"type": "item.completed", "item": {"type": "command_execution", "id": "a",
                                                    "aggregated_output": MARK}},
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
        routes, leaks = {}, []
        for case, job in jobs.items():
            public, fleet, feed, route = self._surfaces(job)
            routes[case] = route
            for place, value in (("job_json", json.dumps(public, ensure_ascii=False)),
                                 ("fleet_rows", fleet),
                                 ("shell_feed", json.dumps(feed, ensure_ascii=False))):
                if MARK in value or B64 in value:
                    leaks.append((case, place, value))
        painted = self._paint(routes)
        for case, widths in painted.items():
            for width, text in widths.items():
                if MARK in text or B64 in text:
                    leaks.append((case, "panel_%s" % width, text))
        self.assertEqual(leaks, [])
        # The surfaces were really drawn: the allow-listed summaries are there.
        self.assertEqual(jobs["claude:edit_body"].exec_tool["target"], "src/app.py")
        self.assertEqual(jobs["claude:edit_body"].last_tool, {"name": "Edit", "target": "src/app.py"})
        self.assertEqual(jobs["claude:pipe_tail"].exec_tool, {"name": "cat", "target": "notes.txt",
                                                              "since": 1794445200.0})
        self.assertEqual(jobs["codex-wrapped:export_then"].exec_tool, {"name": "make", "target": "build"})
        self.assertIn("after Edit · src/app.py", painted["claude:edit_body"]["40"].replace("\n", ""))
        self.assertIn(">_ cat · notes.txt", painted["claude:pipe_tail"]["80"])

    def _paint(self, routes):
        node = shutil.which("node")
        if node is None:
            self.skipTest("node is not available")
        source = Path(self.tmp.name, "routes.json")
        source.write_text(json.dumps(routes, ensure_ascii=False))
        script = r"""
const fs=require('fs'),view=require(process.argv[1]),routes=JSON.parse(fs.readFileSync(process.argv[2],'utf8'));
const out={};for(const [key,route] of Object.entries(routes)){out[key]={};for(const width of [40,80]){
 const x=view.render({width,height:40,side:width<80,model:{routes:[route],route_count:1,messages:[]},sid:'s',
  configuration:{},expandedKeys:['route:r'],questions:()=>[]});
 out[key][width]=x.rows.map(r=>r.map(p=>p.text).join('')).join('\n');}}
process.stdout.write(JSON.stringify(out));"""
        result = subprocess.run(
            [node, "-e", script, str(ROOT / "utilities/session-shell/panel-view.cjs"), str(source)],
            capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)


if __name__ == "__main__":
    unittest.main()
