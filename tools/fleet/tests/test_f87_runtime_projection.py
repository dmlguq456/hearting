import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
import unittest.mock
from pathlib import Path

from tools.fleet.herdr_projection import compose
from tools.fleet.session_handle import display_name, minted_tag

CODEX_SID = "01a0c233-96b3-7cf3-871b-7beb1fc71679"   # a rollout name must end in a uuid
ROOT = next(parent for parent in Path(__file__).resolve().parents
            if (parent / "adapters/codex").is_dir())
STATUSLINE = ROOT / "adapters/claude/statusline.sh"
HELPER = ROOT / "adapters/claude/tools/fleet/session_handle.py"


class RuntimeProjectionTest(unittest.TestCase):
    # Whoever runs this suite may themselves BE a registered worker (a dispatched
    # reviewer, the title refresher). Those markers gate the projection, so leaving them
    # inherited makes the result depend on who ran the test.
    _WORKER_ENV = ("AGENT_SESSION_ROLE", "AGENT_DISPATCH_CHILD", "AGENT_DISPATCH_DEPTH",
                   "OPENCODE_DISPATCH_SLUG", "FLEET_TITLE_REFRESH", "MEM_DISTILL")

    def env(self, root, **overrides):
        env = os.environ.copy()
        for name in self._WORKER_ENV:
            env.pop(name, None)
        env.update({"AGENT_HOME": str(root / "agent"), "HOME": str(root / "home"),
                    "CLAUDE_CONFIG_DIR": str(root / "home" / ".claude"),
                    "HARNESS_CAPACITY_REFRESH_DISABLE": "1",
                    "CODEX_HOME": str(root / "codex"), "FLEET_TITLE_STATE_DIR": str(root / "titles"),
                    "PYTHONDONTWRITEBYTECODE": "1"})
        env.pop("CODEX_THREAD_ID", None)
        env.update(overrides)
        return env

    def own_claude_session(self, root, sid):
        """Make THIS test process a Claude runtime on ``sid`` for its children: the
        projection only reports a session whose runtime is an ancestor of the hook, and
        it reads that from `<CLAUDE_CONFIG_DIR>/sessions/<pid>.json`, like Claude Code."""
        sessions = Path(self.env(root)["CLAUDE_CONFIG_DIR"]) / "sessions"
        sessions.mkdir(parents=True, exist_ok=True)
        (sessions / ("%d.json" % os.getpid())).write_text(json.dumps({"sessionId": sid}))

    def own_codex_thread(self, root, bindir, sid):
        """A process named ``codex`` holding ``sid``'s rollout open, as a real Codex runtime
        does: returns ``(interpreter, prelude)`` — run the prelude first in that
        interpreter so the rollout fd stays open while the hook's ancestors are walked."""
        rollout = (root / "codex" / "sessions" / "2026" / "09" / "25"
                   / ("rollout-2026-09-25T00-00-00-%s.jsonl" % sid))
        rollout.parent.mkdir(parents=True, exist_ok=True)
        rollout.write_text(json.dumps({"type": "session_meta",
                                       "payload": {"id": sid, "cwd": str(root)}}) + "\n")
        interpreter = bindir / "codex"
        if not os.path.lexists(interpreter):
            os.symlink(sys.executable, interpreter)
        return str(interpreter), "_held=open(%r);" % str(rollout)

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

    def project(self, root, sid=CODEX_SID, mode="ok", worker=False, title="title",
                formatter=None, harness="codex"):
        bindir, log = self.stub(root)
        sidecar = root / "titles" / harness / (sid + ".json")
        sidecar.parent.mkdir(parents=True, exist_ok=True)
        sidecar.write_text(title if title.startswith("{") else json.dumps({"title": title}))
        log.write_text("")
        env = self.env(root)
        env.update({"PATH": str(bindir) + os.pathsep + env["PATH"], "HERDR_PANE_ID": "pane-7", "HERDR_LOG": str(log), "HERDR_MODE": mode, "HERDR_EXIT": "7" if mode == "nonzero" else "0"})
        # The reporting process must be the session's own runtime (may_report): Codex by
        # the rollout it holds open, Claude by its session file, OpenCode by the process name.
        interpreter, prelude = sys.executable, ""
        if harness == "codex":
            interpreter, prelude = self.own_codex_thread(root, bindir, sid)
        elif harness == "claude":
            self.own_claude_session(root, sid)
        elif harness == "opencode":
            interpreter = str(bindir / "opencode")
            if not os.path.lexists(interpreter):
                os.symlink(sys.executable, interpreter)
        if formatter is not None:
            env["HERDR_SESSION_METADATA_FORMATTER"] = str(formatter)
        if harness == "codex":
            # Through the Codex adapter hook, which is the entry point its two lifecycle
            # hooks call — proving the wrapper still reaches the shared projector.
            code = (prelude + "import sys;sys.path.insert(0,%r);from adapters.codex.hooks.herdr_session_projection import project;assert project({},%r,worker=%r)"
                    % (str(ROOT), sid, worker))
        else:
            code = ("import sys;sys.path.insert(0,%r);from tools.fleet.herdr_projection import project;assert project(%r,%r,worker=%r)"
                    % (str(ROOT), harness, sid, worker))
        result = subprocess.run([interpreter, "-c", code], env=env, capture_output=True)
        rows = [json.loads(x) for x in log.read_text().splitlines()] if log.exists() else []
        return result, rows

    def claude_hook(self, root, sid, payload=None, **env_overrides):
        """The hearting-owned Claude hook — herdr's own integration reports state and the
        session id, and never a title, so a Claude pane header had nothing on it."""
        bindir, log = self.stub(root)
        env = self.env(root, **env_overrides)
        env.update({"PATH": str(bindir) + os.pathsep + env["PATH"], "HERDR_PANE_ID": "pane-7",
                    "HERDR_LOG": str(log), "HERDR_MODE": "ok", "HERDR_EXIT": "0"})
        if isinstance(sid, str) and sid:
            self.own_claude_session(root, sid)
        log.write_text("")
        body = json.dumps(payload if payload is not None else {"session_id": sid})
        result = subprocess.run([sys.executable, str(ROOT / "hooks/herdr-session-projection.py")],
                                input=body, text=True, capture_output=True, env=env)
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
        sid = CODEX_SID
        agent = "[%s] codex" % minted_tag(sid)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _, rows = self.project(root, sid, title="A title")
            self.assertEqual(rows, [["pane","report-agent-session","pane-7","--source","herdr:codex","--agent","codex","--agent-session-id",sid], ["pane","report-metadata","pane-7","--source","herdr:codex","--display-agent",agent,"--title",agent + " A title"]])
            _, rows = self.project(root, sid, title="{")
            # No summary is not "no header": the number still has to reach the pane.
            self.assertEqual(rows[1][rows[1].index("--title") + 1], agent)
            _, rows = self.project(root, sid, title="가" * 60)
            projected = rows[1][rows[1].index("--title") + 1]
            # Identity first and whole; the summary is what yields to the budget.
            self.assertTrue(projected.startswith(agent + " "), projected)
            self.assertTrue(projected.endswith("…"), projected)
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
        sid = CODEX_SID
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
            # A personal formatter renames the middle harness word only: the `[tag]` badge
            # and the steward mark are hearting's, composed OUTSIDE the formatter result,
            # so a formatter cannot quietly delete the two things that identify a session.
            self.assertEqual(rows[1][6:],
                             ["[%s] codex" % minted_tag(sid), "--title",
                              "[%s] codex Session summary" % minted_tag(sid)])

            formatter.write_text("#!/usr/bin/env python3\nprint('{')\n")
            formatter.chmod(formatter.stat().st_mode | stat.S_IXUSR)
            _, rows = self.project(root, sid, title="Fallback", formatter=formatter)
            self.assertEqual(rows[1][6:],
                             ["[%s] codex" % minted_tag(sid), "--title",
                              "[%s] codex Fallback" % minted_tag(sid)])

            formatter.write_text("#!/usr/bin/env python3\nimport time;time.sleep(.5)\n")
            formatter.chmod(formatter.stat().st_mode | stat.S_IXUSR)
            _, rows = self.project(root, sid, title="Timeout", formatter=formatter)
            self.assertEqual(rows[1][6:],
                             ["[%s] codex" % minted_tag(sid), "--title",
                              "[%s] codex Timeout" % minted_tag(sid)])

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
        """F-99e — statusline and the pane header carry the same one title for one
        session, with zero sid8 handles anywhere."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for harness, sid, title in (("claude","abcdefgh-claude","Task"),("codex",CODEX_SID,"Task")):
                expected = display_name(harness, sid, runtime_name=None, registry_name=None,
                                        title=title, slug=None, cwd=None)
                self.assertEqual(expected, title)
                if harness == "claude":
                    self.assertIn(expected, self.statusline(root, sid, title).stdout)
                else:
                    _, rows = self.project(root, sid, title=title)
                    self.assertEqual(rows[1][-1], "[%s] codex %s" % (minted_tag(sid), title))

    def test_pane_header_order_is_number_then_harness_then_steward(self):
        """User-fixed 2026-09-09 format `[번호] 하네스 (⚑) 요약`, one shape everywhere."""
        for harness in ("claude", "codex", "opencode"):
            self.assertEqual(compose(harness, "s", tag="3a", steward=False, title="사이클"),
                             ("[3a] %s" % harness, "사이클"))
            self.assertEqual(compose(harness, "s", tag="b0", steward=True, title="감독")[0],
                             "[b0] %s ⚑" % harness)
        # No tag resolves: the badge slot is dropped rather than shown empty or faked.
        self.assertEqual(compose("claude", "s", tag=None, steward=False, title="t")[0],
                         "claude")
        # Budgets are herdr's; a long title is clipped, never wrapped into the agent cell.
        agent, title = compose("codex", "s", tag="3a", steward=True, title="가" * 80)
        self.assertEqual(agent, "[3a] codex ⚑")
        self.assertLess(len(title), 80)

    def test_claude_hook_reports_metadata_and_leaves_the_session_id_to_herdr(self):
        """`report-agent-session` stays herdr's own integration's job — two sources
        claiming one pane's agent session would race their `seq` values."""
        with tempfile.TemporaryDirectory() as td:
            root, sid = Path(td), "abcdefgh-claude"
            sidecar = root / "titles/claude" / (sid + ".json")
            sidecar.parent.mkdir(parents=True, exist_ok=True)
            sidecar.write_text(json.dumps({"title": "Claude pane title"}))
            result, rows = self.claude_hook(root, sid)
            self.assertEqual(result.returncode, 0)
            self.assertEqual(result.stdout, "")
            self.assertEqual([row[1] for row in rows], ["report-metadata"])
            self.assertEqual(rows[0][4:], ["herdr:claude", "--display-agent", "claude",
                                           "--title", "claude Claude pane title"])

    def test_claude_hook_skips_a_registered_worker(self):
        with tempfile.TemporaryDirectory() as td:
            root, sid = Path(td), "abcdefgh-claude"
            for marker in ("AGENT_SESSION_ROLE", "AGENT_DISPATCH_DEPTH"):
                value = "worker" if marker == "AGENT_SESSION_ROLE" else "2"
                _, rows = self.claude_hook(root, sid, **{marker: value})
                self.assertEqual(rows, [], marker)

    def test_claude_hook_is_fail_soft_on_every_bad_input(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for payload in ({}, {"session_id": ""}, {"session_id": 7}):
                result, rows = self.claude_hook(root, "unused", payload=payload)
                self.assertEqual(result.returncode, 0)
                self.assertEqual(rows, [])
            proc = subprocess.run([sys.executable, str(ROOT / "hooks/herdr-session-projection.py")],
                                  input="{bad", text=True, capture_output=True,
                                  env=self.env(root))
            self.assertEqual(proc.returncode, 0)

    def test_every_harness_skips_a_registered_worker(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for harness in ("claude", "codex", "opencode"):
                _, rows = self.project(root, CODEX_SID if harness == "codex" else "abcdefgh-%s" % harness, harness=harness,
                                       worker=True)
                self.assertEqual(rows, [], harness)

    def test_codex_prompt_hook_with_a_fake_session_never_reaches_the_live_pane(self):
        """The 2026-09-24 leak: `portable-guards.test.sh` fed `directpromptsid` to the Codex
        prompt hook while inheriting the pane's HERDR_*, and a live header read
        `[0d] codex`. The hook's process is not that session's runtime, so nothing is sent
        — neither the header nor `report-agent-session`. Asserted in every case, whatever
        runtime happens to run this suite: a Codex hook gets no `CODEX_THREAD_ID`
        (measured 2026-09-25), and when one is set it proves nothing either."""
        hook = ROOT / "adapters/codex/hooks/userprompt-lifecycle.py"
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            bindir, log = self.stub(root)
            interpreter, prelude = self.own_codex_thread(root, bindir, CODEX_SID)
            runner = (prelude + "import subprocess,sys;subprocess.run([%r,%r],input=sys.argv[1],"
                      "text=True,capture_output=True,timeout=60)" % (sys.executable, str(hook)))
            cases = (
                # (payload session, CODEX_THREAD_ID, a codex ancestor holds CODEX_SID?, reported)
                ("directpromptsid", None, True, False),
                ("directpromptsid", "directpromptsid", True, False),
                ("directpromptsid", None, False, False),
                ("directpromptsid", "directpromptsid", False, False),
                (CODEX_SID, None, False, False),
                (CODEX_SID, None, True, True),
            )
            for session, thread, held, reported in cases:
                env = self.env(root)
                env.update({"PATH": str(bindir) + os.pathsep + env["PATH"],
                            "HERDR_ENV": "1", "HERDR_PANE_ID": "wB:pN",
                            "HERDR_SOCKET_PATH": str(root / "live.sock"),
                            "HERDR_LOG": str(log), "HERDR_MODE": "ok", "HERDR_EXIT": "0"})
                if thread:
                    env["CODEX_THREAD_ID"] = thread
                payload = json.dumps({"prompt": "deterministic recall", "session_id": session,
                                      "turn_id": "directturnid", "cwd": ""})
                log.write_text("")
                subprocess.run([interpreter if held else sys.executable, "-c",
                                runner if held else runner[len(prelude):], payload],
                               env=env, capture_output=True, timeout=90, check=True)
                case = (session, thread, held)
                if reported:
                    self.assertIn("report-agent-session", log.read_text(), case)
                else:
                    self.assertEqual(log.read_text(), "", case)

    def test_identity_guard_rejects_a_foreign_session_and_reports_the_own_one(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            bindir, log = self.stub(root)
            self.own_claude_session(root, "real-claude-session")
            env = self.env(root)
            env.update({"PATH": str(bindir) + os.pathsep + env["PATH"], "HERDR_PANE_ID": "pane-7",
                        "HERDR_LOG": str(log), "HERDR_MODE": "ok", "HERDR_EXIT": "0"})
            code = ("import sys;sys.path.insert(0,%r);from tools.fleet.herdr_projection import project;"
                    "project('claude', sys.argv[1], worker=False)" % str(ROOT))
            for sid, reported in (("foreign-session", False), ("real-claude-session", True)):
                log.write_text("")
                subprocess.run([sys.executable, "-c", code, sid], env=env, check=True)
                rows = [json.loads(x) for x in log.read_text().splitlines()]
                self.assertEqual(bool(rows), reported, sid)
                if reported:
                    self.assertIn("--agent-session-id", rows[0])
                    self.assertEqual(rows[0][rows[0].index("--agent-session-id") + 1], sid)
            # Codex: the rollout the runtime holds open decides, whatever the payload or
            # CODEX_THREAD_ID says.
            interpreter, prelude = self.own_codex_thread(root, bindir, CODEX_SID)
            env["CODEX_THREAD_ID"] = "directpromptsid"
            code = prelude + code.replace("'claude'", "'codex'")
            for sid, reported in (("directpromptsid", False), (CODEX_SID, True)):
                log.write_text("")
                subprocess.run([interpreter, "-c", code, sid], env=env, check=True)
                self.assertEqual(bool(log.read_text().strip()), reported, sid)

    def test_a_claude_session_whose_registry_file_vanished_still_reports(self):
        """F-25 tier 2: `sessions/<pid>.json` can vanish for hours while the process lives.
        The statusline tap matched by pid + start time is the same recovery Fleet's
        collector uses, so the real session keeps its header; a foreign id is still
        refused, and a tap for another start time (a recycled pid) proves nothing."""
        from tools.fleet.collectors.procscan import read_proc_start
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            bindir, log = self.stub(root)
            config = Path(self.env(root)["CLAUDE_CONFIG_DIR"])
            self.own_claude_session(root, "real-claude-session")
            (config / "sessions" / ("%d.json" % os.getpid())).unlink()
            taps = config / ".statusline"
            taps.mkdir(parents=True)
            env = self.env(root)
            env.update({"PATH": str(bindir) + os.pathsep + env["PATH"], "HERDR_PANE_ID": "pane-7",
                        "HERDR_LOG": str(log), "HERDR_MODE": "ok", "HERDR_EXIT": "0"})
            code = ("import sys;sys.path.insert(0,%r);from tools.fleet.herdr_projection import project;"
                    "project('claude', sys.argv[1], worker=False)" % str(ROOT))

            def reported(sid):
                log.write_text("")
                subprocess.run([sys.executable, "-c", code, sid], env=env, check=True)
                return bool(log.read_text().strip())

            self.assertFalse(reported("real-claude-session"))   # no registry, no tap
            tap = taps / "real-claude-session.json"
            tap.write_text(json.dumps({"pid": os.getpid(), "proc_start": "1",
                                       "session_id": "real-claude-session"}))
            self.assertFalse(reported("real-claude-session"))   # another process's start time
            tap.write_text(json.dumps({"pid": os.getpid(),
                                       "proc_start": read_proc_start(os.getpid()),
                                       "session_id": "real-claude-session"}))
            self.assertTrue(reported("real-claude-session"))
            self.assertFalse(reported("foreign-session"))
            # A registry row left by a recycled pid is not this process's session either.
            (config / "sessions" / ("%d.json" % os.getpid())).write_text(
                json.dumps({"sessionId": "foreign-session", "procStart": "1"}))
            self.assertFalse(reported("foreign-session"))
            self.assertTrue(reported("real-claude-session"))

    def test_no_runtime_identity_means_no_report(self):
        """CI, a detached helper, a fake config dir: nothing to prove → nothing sent."""
        from tools.fleet import herdr_projection as hp
        with unittest.mock.patch.object(hp, "runtime_identity", return_value=(None, None)), \
                unittest.mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CODEX_THREAD_ID", None)
            for harness in hp.HARNESSES:
                self.assertFalse(hp.may_report(harness, "abcdefgh-1", worker=False), harness)
        with unittest.mock.patch.object(hp, "runtime_identity", return_value=("claude", "s-1")):
            self.assertTrue(hp.may_report("claude", "s-1", worker=False))
            self.assertFalse(hp.may_report("claude", "s-2", worker=False))
            self.assertFalse(hp.may_report("opencode", "s-1", worker=False))
            self.assertFalse(hp.may_report("claude", "s-1", worker=True))

    def test_opencode_projects_through_the_same_shared_surface(self):
        sid = "abcdefgh-opencode"
        with tempfile.TemporaryDirectory() as td:
            _, rows = self.project(Path(td), sid, harness="opencode", title="OC task")
            oc_agent = "[%s] opencode" % minted_tag(sid)
            self.assertEqual(rows[-1][4:], ["herdr:opencode", "--display-agent", oc_agent,
                                            "--title", oc_agent + " OC task"])

    def test_statusline_carries_the_title_only(self):
        """2026-09-09 — the `[46]` badge moved to the pane header, and a name that is just
        the folder is not a title (the `📁 <dir>` segment already says it)."""
        with tempfile.TemporaryDirectory() as td:
            root, sid = Path(td), "abcdefgh-claude"
            import re
            strip = lambda s: re.sub(r"\x1b\[[0-9;]*m", "", s)
            out = strip(self.statusline(root, sid, "Real title").stdout)
            segment = next(s for s in out.split("│") if "Real title" in s)
            self.assertEqual(segment.strip(), "Real title")   # title only, no `[46]` badge
            # A name that is only the folder is not a title: `📁 <dir>` already says it.
            bare = strip(self.statusline(root, sid, root.name).stdout)
            self.assertEqual(bare.count(root.name), 1)

    def test_sessionstart_worker_gating_and_json_contract(self):
        env = {**os.environ, "AGENT_SESSION_ROLE": "worker", "HERDR_PANE_ID": "pane-7"}
        result = subprocess.run([sys.executable, str(ROOT / "adapters/codex/hooks/sessionstart-lifecycle.py")], input=json.dumps({"session_id":"abcdefgh-123"}), text=True, capture_output=True, env=env)
        self.assertEqual(result.returncode, 0)
        if result.stdout.strip(): json.loads(result.stdout)

    def test_statusline_malformed_input_exits_zero(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(subprocess.run([str(STATUSLINE)], input="{bad", text=True, capture_output=True, env={**os.environ, "AGENT_HOME": td}).returncode, 0)


class PaneHeaderIdentityTest(unittest.TestCase):
    """The number has to be in the field herdr actually paints.

    `display_agent` is reported and herdr stores it, but it does not reach the pane
    header. Measured 2026-09-10: four panes held `[6b] claude` in `display_agent` for
    hours while their headers showed no identity, and a header only began saying
    something once `title` was filled — the user watched that exact A/B and reported
    "제목은 뜨는데 id는 여전히 안뜨는데. 그게 중요한건데".
    """

    def test_the_header_leads_with_the_number_then_harness_then_summary(self):
        from tools.fleet.herdr_projection import header_title
        self.assertEqual(header_title("[6b] claude", "r1-model S8 재진입 버그 수정"),
                         "[6b] claude r1-model S8 재진입 버그 수정")

    def test_the_steward_mark_rides_with_the_identity(self):
        from tools.fleet.herdr_projection import header_title
        self.assertTrue(header_title("[94] claude ⚑", "v6 Release").startswith(
            "[94] claude ⚑ "))

    def test_an_identity_with_no_summary_still_shows_the_number(self):
        # The number is the part the user called the important one; a session whose
        # title worker has produced nothing must not go anonymous because of it.
        from tools.fleet.herdr_projection import header_title
        self.assertEqual(header_title("[6b] claude", ""), "[6b] claude")

    def test_a_long_summary_is_clipped_and_the_identity_is_not(self):
        from tools.fleet.herdr_projection import header_title
        from tools.fleet.session_handle import _cell_width
        header = header_title("[6b] claude", "장" * 200)
        self.assertTrue(header.startswith("[6b] claude "))
        self.assertLessEqual(_cell_width(header), 72)

    def test_both_fields_are_reported_in_one_call(self):
        """herdr's metadata record is per-source and a report replaces it whole — sending
        `--title` alone clears `--display-agent` (measured on a live pane)."""
        import inspect
        from tools.fleet import herdr_projection
        body = inspect.getsource(herdr_projection.project)
        metadata = body.split("metadata = [", 1)[1].split("\n\n", 1)[0]
        self.assertIn("--display-agent", metadata)
        self.assertIn("--title", metadata)


class PaneTitleLadderTest(unittest.TestCase):
    """The pane header and the board must climb ONE ladder for the same session.

    Measured 2026-09-10: four of six Claude panes had a badge and no title at all, while
    the board named every one of them. The board's ladder is fresh sidecar → the
    transcript's own ai-title → the runtime's session name; the pane header stopped at the
    first rung, so any session whose title worker had failed (`summary_failures: 3` on two
    of them) went anonymous in its pane.
    """

    SID = "11111111-2222-3333-4444-555555555555"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "claude"
        (self.home / "projects" / "-x-repo").mkdir(parents=True)
        self.transcript = self.home / "projects" / "-x-repo" / (self.SID + ".jsonl")
        # Real files, not a patched module: `herdr_projection` imports `fleet.titles`
        # while this file imports `tools.fleet.titles`, and those are two module objects
        # for one source file — patching one leaves the other untouched.
        self._patch = unittest.mock.patch.dict(
            os.environ, {"CLAUDE_CONFIG_DIR": str(self.home),
                         "XDG_STATE_HOME": str(Path(self.tmp.name) / "state")})
        self._patch.start()
        self.addCleanup(self._patch.stop)

    def _write_transcript(self, title):
        self.transcript.write_text(
            json.dumps({"type": "ai-title", "aiTitle": title}) + "\n", encoding="utf-8")

    def _write_sidecar(self, title, age_sec=0):
        from tools.fleet import titles
        path = Path(titles.sidecar_path(self.SID, harness="claude"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"title": title, "ts": time.time() - age_sec}),
                        encoding="utf-8")

    def _title(self):
        from tools.fleet.herdr_projection import session_title
        return session_title("claude", self.SID)

    def test_the_transcript_title_is_used_when_the_sidecar_has_none(self):
        # The real case: the title worker ran and failed, leaving `title: ""`.
        self._write_sidecar("")
        self._write_transcript("r1-model S8 재진입 버그 수정")
        self.assertEqual(self._title(), "r1-model S8 재진입 버그 수정")

    def test_a_fresh_sidecar_still_outranks_the_transcript(self):
        # Order matters: the sidecar is the worker's considered summary, the ai-title is
        # whatever the runtime named the session first.
        self._write_transcript("older transcript title")
        self._write_sidecar("v6 Command Model Release")
        self.assertEqual(self._title(), "v6 Command Model Release")

    def test_a_stale_sidecar_yields_to_the_transcript_but_beats_nothing(self):
        # The board drops a sidecar this old; the header keeps it rather than going blank,
        # but only once the rung the board WOULD have used has been tried.
        self._write_sidecar("aged summary", age_sec=86400)
        self._write_transcript("current transcript title")
        self.assertEqual(self._title(), "current transcript title")
        self.transcript.unlink()
        self.assertEqual(self._title(), "aged summary")

    def test_no_title_anywhere_is_empty_not_a_placeholder(self):
        # A pane header saying "?" or repeating the folder is worse than one saying
        # nothing — herdr already shows the folder beside it.
        self.assertEqual(self._title(), "")

    def test_the_locator_never_borrows_a_neighbour_transcript(self):
        from tools.fleet.collectors.claude import ai_title_for_session
        neighbour = self.home / "projects" / "-x-repo" / "99999999-0000-0000-0000-000000000000.jsonl"
        neighbour.write_text(
            json.dumps({"type": "ai-title", "aiTitle": "someone else's session"}) + "\n",
            encoding="utf-8")
        self.assertIsNone(ai_title_for_session(self.SID, home=str(self.home)))

    def test_a_missing_id_or_home_is_none_not_a_crash(self):
        from tools.fleet.collectors.claude import ai_title_for_session
        for value in (None, "", 42):
            with self.subTest(value=value):
                self.assertIsNone(ai_title_for_session(value))


if __name__ == "__main__":
    unittest.main()
