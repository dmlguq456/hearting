import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tools.fleet.session_handle import display_name

ROOT = next(parent for parent in Path(__file__).resolve().parents
            if (parent / "adapters/codex").is_dir())
STATUSLINE = ROOT / "adapters/claude/statusline.sh"
HELPER = ROOT / "adapters/claude/tools/fleet/session_handle.py"


class RuntimeProjectionTest(unittest.TestCase):
    def env(self, root):
        env = os.environ.copy()
        env.update({"AGENT_HOME": str(root / "agent"), "HOME": str(root / "home"),
                    "CODEX_HOME": str(root / "codex"), "FLEET_TITLE_STATE_DIR": str(root / "titles"),
                    "PYTHONDONTWRITEBYTECODE": "1"})
        return env

    def statusline(self, root, sid, title, helper=True):
        path = Path(self.env(root)["AGENT_HOME"]) / "tools/fleet/session_handle.py"
        if helper:
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(HELPER, path)
        else:
            path.unlink(missing_ok=True)
        return subprocess.run([str(STATUSLINE)], input=json.dumps({"cwd": str(root), "session_id": sid, "session_name": title}), text=True, capture_output=True, env=self.env(root))

    def stub(self, root):
        bindir, log = root / "bin", root / "herdr.jsonl"
        bindir.mkdir(exist_ok=True)
        path = bindir / "herdr"
        path.write_text("#!/usr/bin/env python3\nimport json,os,sys,time\nwith open(os.environ['HERDR_LOG'],'a') as f: f.write(json.dumps(sys.argv[1:])+'\\n')\nif os.environ.get('HERDR_MODE')=='timeout': time.sleep(.8)\nraise SystemExit(int(os.environ.get('HERDR_EXIT','0')))\n")
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
        return bindir, log

    def project(self, root, sid="abcdefgh-123", mode="ok", worker=False, title="title",
                formatter=None):
        bindir, log = self.stub(root)
        sidecar = root / "titles/codex" / (sid + ".json")
        sidecar.parent.mkdir(parents=True, exist_ok=True)
        sidecar.write_text(title if title.startswith("{") else json.dumps({"title": title}))
        log.write_text("")
        env = self.env(root)
        env.update({"PATH": str(bindir) + os.pathsep + env["PATH"], "HERDR_PANE_ID": "pane-7", "HERDR_LOG": str(log), "HERDR_MODE": mode, "HERDR_EXIT": "7" if mode == "nonzero" else "0"})
        if formatter is not None:
            env["HERDR_SESSION_METADATA_FORMATTER"] = str(formatter)
        code = ("import sys;sys.path.insert(0,%r);from adapters.codex.hooks.herdr_session_projection import project;assert project({},%r,worker=%r)" % (str(ROOT), sid, worker))
        result = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True)
        rows = [json.loads(x) for x in log.read_text().splitlines()] if log.exists() else []
        return result, rows

    def test_claude_positive_missing_control_and_long_titles(self):
        """F-99 — statusline shows the canonical name with zero sid8 (`CL/<sid8>`)."""
        with tempfile.TemporaryDirectory() as td:
            root, sid = Path(td), "abcdefgh-claude"
            self.assertIn("My Task", self.statusline(root, sid, "My Task").stdout)
            missing = self.statusline(root, sid, "My Task", helper=False)
            self.assertEqual(missing.returncode, 0)
            self.assertNotIn("My Task", missing.stdout)
            self.assertNotIn("CL/abcdefgh", missing.stdout)
            self.assertIn("A B", self.statusline(root, sid, "A\nB\x00").stdout)
            long = self.statusline(root, sid, "가" * 100).stdout
            self.assertNotIn("CL/abcdefgh", long)
            display = next(x for x in long.split(" │ ") if "가" in x)
            self.assertLess(display.count("가"), 100)
            self.assertIn("…", display)

    def test_codex_herdr_exact_argv_and_failures(self):
        sid = "abcdefgh-codex"
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _, rows = self.project(root, sid, title="A title")
            self.assertEqual(rows, [["pane","report-agent-session","pane-7","--source","herdr:codex","--agent","codex","--agent-session-id",sid], ["pane","report-metadata","pane-7","--source","herdr:codex","--display-agent","A title","--title","A title"]])
            _, rows = self.project(root, sid, title="{")
            self.assertNotIn("--title", rows[1])
            _, rows = self.project(root, sid, title="가" * 60)
            projected = rows[1][rows[1].index("--title") + 1]
            self.assertEqual(projected, "가" * 23 + "…")
            for mode in ("nonzero", "timeout"):
                _, rows = self.project(root, sid, mode=mode)
                self.assertEqual(len(rows), 2)
            before = root / "codex/config.toml"
            self.assertFalse(before.exists())
            _, rows = self.project(root, sid, worker=True)
            self.assertEqual(rows, [])
            self.assertFalse(before.exists())
            self.assertTrue(all(row[0] == "pane" for row in rows))

    def test_codex_private_metadata_formatter_and_fail_soft_fallback(self):
        sid = "abcdefgh-codex"
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            formatter = root / "formatter"
            formatter.write_text(
                "#!/usr/bin/env python3\n"
                "import argparse,json\n"
                "p=argparse.ArgumentParser();p.add_argument('--harness');"
                "p.add_argument('--session-id');p.add_argument('--summary');a=p.parse_args()\n"
                "print(json.dumps({'display_agent':a.harness,'title':a.summary}))\n"
            )
            formatter.chmod(formatter.stat().st_mode | stat.S_IXUSR)
            _, rows = self.project(root, sid, title="Session summary", formatter=formatter)
            self.assertEqual(rows[1][6:],
                             ["codex", "--title", "Session summary"])

            formatter.write_text("#!/usr/bin/env python3\nprint('{')\n")
            formatter.chmod(formatter.stat().st_mode | stat.S_IXUSR)
            _, rows = self.project(root, sid, title="Fallback", formatter=formatter)
            self.assertEqual(rows[1][6:], ["Fallback", "--title", "Fallback"])

            formatter.write_text("#!/usr/bin/env python3\nimport time;time.sleep(.5)\n")
            formatter.chmod(formatter.stat().st_mode | stat.S_IXUSR)
            _, rows = self.project(root, sid, title="Timeout", formatter=formatter)
            self.assertEqual(rows[1][6:], ["Timeout", "--title", "Timeout"])

    def test_codex_absent_command_is_fail_soft(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            bindir, log = self.stub(root)
            (bindir / "herdr").unlink()
            env = self.env(root)
            env.update({"PATH": str(bindir), "HERDR_PANE_ID": "pane-7"})
            code = ("import sys;sys.path.insert(0,%r);from adapters.codex.hooks.herdr_session_projection import project;assert project({},'abcdefgh-123')" % str(ROOT))
            self.assertEqual(subprocess.run([sys.executable, "-c", code], env=env).returncode, 0)
            self.assertFalse(log.exists())

    def test_shared_input_vectors_match_fleet_claude_and_codex(self):
        """F-99e — statusline and the Herdr formatter both resolve to the same
        `display_name()` output for one title, with zero sid8 handles anywhere."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for harness, sid, title in (("claude","abcdefgh-claude","Task"),("codex","abcdefgh-codex","Task")):
                expected = display_name(harness, sid, runtime_name=None, registry_name=None,
                                        title=title, slug=None, cwd=None)
                self.assertEqual(expected, title)
                if harness == "claude":
                    self.assertIn(expected, self.statusline(root, sid, title).stdout)
                else:
                    _, rows = self.project(root, sid, title=title)
                    self.assertEqual(rows[1][-1], title)
                    self.assertEqual(rows[1][6], expected)

    def test_sessionstart_worker_gating_and_json_contract(self):
        env = {**os.environ, "AGENT_SESSION_ROLE": "worker", "HERDR_PANE_ID": "pane-7"}
        result = subprocess.run([sys.executable, str(ROOT / "adapters/codex/hooks/sessionstart-lifecycle.py")], input=json.dumps({"session_id":"abcdefgh-123"}), text=True, capture_output=True, env=env)
        self.assertEqual(result.returncode, 0)
        if result.stdout.strip(): json.loads(result.stdout)

    def test_statusline_malformed_input_exits_zero(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(subprocess.run([str(STATUSLINE)], input="{bad", text=True, capture_output=True, env={**os.environ, "AGENT_HOME": td}).returncode, 0)


if __name__ == "__main__":
    unittest.main()
