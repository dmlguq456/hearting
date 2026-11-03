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
from types import SimpleNamespace

from tools.fleet.herdr_projection import compose
from tools.fleet import render
from tools.fleet.session_handle import current_session_subject, minted_tag

CODEX_SID = "01a0c233-96b3-7cf3-871b-7beb1fc71679"   # a rollout name must end in a uuid


def codex_sid(path):
    from fleet.collectors.codex import _sid
    return _sid(path)


ROOT = next(parent for parent in Path(__file__).resolve().parents
            if (parent / "adapters/codex").is_dir())
STATUSLINE = ROOT / "adapters/claude/statusline.sh"


class RuntimeProjectionTest(unittest.TestCase):
    def setUp(self):
        # This suite verifies native session identity and metadata formatting.
        # Physical ownership has its own real-pty regression suite.
        sys.path.insert(0, str(ROOT / "utilities"))
        import pane_ownership
        proof = unittest.mock.patch.object(pane_ownership, "verified_pane", return_value="fixture-pane")
        proof.start()
        self.addCleanup(proof.stop)
    # Whoever runs this suite may themselves BE a registered worker (a dispatched
    # reviewer, the title refresher). Those markers gate the projection, so leaving them
    # inherited makes the result depend on who ran the test.
    _WORKER_ENV = ("AGENT_SESSION_ROLE", "AGENT_DISPATCH_CHILD", "AGENT_DISPATCH_DEPTH",
                   "OPENCODE_DISPATCH_SLUG", "FLEET_TITLE_REFRESH", "MEM_DISTILL")

    def env(self, root, **overrides):
        env = os.environ.copy()
        for name in self._WORKER_ENV:
            env.pop(name, None)
        # A caller's live state roots take precedence over this fixture's HOME /
        # XDG_STATE_HOME (state_root() reads HARNESS_STATE_ROOT and the inherited
        # dispatch registry first), so clear them or a fake-session hook writes
        # a real recipient inbox.
        for name in ("HARNESS_STATE_ROOT", "AGENT_DISPATCH_JOBS",
                     "AGENT_PEER_LEDGER_ROOT"):
            env.pop(name, None)
        env.update({"AGENT_HOME": str(root / "agent"), "HOME": str(root / "home"),
                    "CLAUDE_CONFIG_DIR": str(root / "home" / ".claude"),
                    "HARNESS_CAPACITY_REFRESH_DISABLE": "1",
                    "CODEX_HOME": str(root / "codex"), "FLEET_TITLE_STATE_DIR": str(root / "titles"),
                    "XDG_STATE_HOME": str(root / "state"),
                    "FLEET_SESSION_REGISTRY_DIR": str(root / "registry"),
                    "PYTHONDONTWRITEBYTECODE": "1"})
        env.pop("CODEX_THREAD_ID", None)
        fixture = root / "python-fixture"
        fixture.mkdir(exist_ok=True)
        (fixture / "sitecustomize.py").write_text(
            f"import os, sys; sys.path.insert(0, {str(ROOT / 'utilities')!r})\n"
            "import pane_ownership\n"
            "def admitted(pane,harness,sid=None,*,pid=None,**kw):\n"
            "    if harness == 'codex':\n"
            "        # Stand-in for the two-read footer proof: the footer names the thread the\n"
            "        # pane has selected (HERDR_CURRENT_FOOTER), by default the one this\n"
            "        # process holds open.\n"
            "        current = os.environ.get('HERDR_CURRENT_PID')\n"
            "        if pid is None or (current is not None and str(pid) != current):\n"
            "            return ''\n"
            "        footer = os.environ.get('HERDR_CURRENT_FOOTER')\n"
            "        if footer is None:\n"
            "            footer = pane_ownership._native_session(pid, 'codex')\n"
            "        return (pane or '') if sid and sid == footer else ''\n"
            "    if pid is not None and sid and pane_ownership._native_session(pid,harness)!=sid:\n"
            "        return ''\n"
            "    return pane or ''\n"
            "pane_ownership.verified_pane=admitted\n")
        env["PYTHONPATH"] = str(fixture) + os.pathsep + env.get("PYTHONPATH", "")
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

    def statusline(self, root, sid, native_session_name, helper=True):
        package = Path(self.env(root)["AGENT_HOME"]) / "tools/fleet"
        package.mkdir(parents=True, exist_ok=True)
        for module in ("__init__.py", "titles.py"):
            shutil.copyfile(ROOT / "tools/fleet" / module, package / module)
        path = package / "session_handle.py"
        if helper:
            shutil.copyfile(ROOT / "tools/fleet/session_handle.py", path)
        else:
            path.unlink(missing_ok=True)
        return subprocess.run([str(STATUSLINE)], input=json.dumps({"cwd": str(root), "session_id": sid, "session_name": native_session_name}), text=True, capture_output=True, env=self.env(root))

    def successful_title(self, root, sid, title):
        path = root / "titles" / "claude" / (sid + ".json")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"title": title}), encoding="utf-8")

    def stub(self, root):
        bindir, log = root / "bin", root / "herdr.jsonl"
        bindir.mkdir(exist_ok=True)
        path = bindir / "herdr"
        path.write_text("#!/usr/bin/env python3\nimport json,os,sys,time\n"
                        # A read-only `pane list` is what the identity walk issues when it
                        # passes a live `claude` ancestor (a suite run from Claude Code); it
                        # reports nothing, so only the commands that would are logged.
                        "if sys.argv[1:3]!=['pane','list']:\n"
                        " with open(os.environ['HERDR_LOG'],'a') as f: f.write(json.dumps(sys.argv[1:])+'\\n')\nif os.environ.get('HERDR_MODE')=='timeout': time.sleep(.8)\nraise SystemExit(int(os.environ.get('HERDR_EXIT','0')))\n")
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
        # Allocation may probe the live inventory. Keep the exact publisher
        # argv/order assertions independent of those read-only observations.
        rows = [row for row in rows if row[:2] in
                (["pane", "report-agent-session"], ["pane", "report-metadata"])]
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
            self.successful_title(root, sid, "My Task")
            native_title = "Automatic stdin title"
            output = self.statusline(root, sid, native_title).stdout
            self.assertIn("My Task", output)
            self.assertNotIn(native_title, output)
            missing = self.statusline(root, sid, native_title, helper=False)
            self.assertEqual(missing.returncode, 0)
            self.assertNotIn("My Task", missing.stdout)
            self.assertNotIn(native_title, missing.stdout)
            self.assertNotIn("CL/abcdefgh", missing.stdout)
            self.successful_title(root, sid, "A\nB\x00")
            self.assertIn("A B", self.statusline(root, sid, native_title).stdout)
            self.successful_title(root, sid, "가" * 100)
            long = self.statusline(root, sid, native_title).stdout
            self.assertNotIn("CL/abcdefgh", long)
            self.assertNotIn(native_title, long)
            display = next(x for x in long.split(" │ ") if "가" in x)
            self.assertLess(display.count("가"), 100)
            self.assertIn("…", display)

    def test_codex_herdr_exact_argv_and_failures(self):
        sid = CODEX_SID
        agent = "[%s] codex" % minted_tag(sid)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _, rows = self.project(root, sid, title="A title")
            self.assertEqual(rows, [
                ["pane", "report-agent-session", "pane-7", "--source", "herdr:codex",
                 "--agent", "codex", "--agent-session-id", sid],
                ["pane", "report-metadata", "pane-7", "--source", "herdr:codex",
                 "--display-agent", agent, "--title", agent + " A title"],
            ])
            _, rows = self.project(root, sid, title="{")
            # No summary is not "no header": the number still has to reach the pane.
            metadata = next(row for row in rows if row[1] == "report-metadata")
            self.assertEqual(metadata[metadata.index("--title") + 1], agent)
            _, rows = self.project(root, sid, title="가" * 60)
            metadata = next(row for row in rows if row[1] == "report-metadata")
            projected = metadata[metadata.index("--title") + 1]
            # Identity first and whole; the summary is what yields to the budget.
            self.assertTrue(projected.startswith(agent + " "), projected)
            self.assertTrue(projected.endswith("…"), projected)
            for mode in ("nonzero", "timeout"):
                result, rows = self.project(root, sid, mode=mode)
                self.assertEqual(result.returncode, 0)
                self.assertEqual([row[1] for row in rows],
                                 ["report-agent-session", "report-metadata"])
                self.assertEqual(rows[0], [
                    "pane", "report-agent-session", "pane-7", "--source", "herdr:codex",
                    "--agent", "codex", "--agent-session-id", sid,
                ])
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
            self.assertEqual([row[1] for row in rows],
                             ["report-agent-session", "report-metadata"])
            metadata = next(row for row in rows if row[1] == "report-metadata")
            # A personal formatter renames the middle harness word only: the `[tag]` badge
            # and the steward mark are hearting's, composed OUTSIDE the formatter result,
            # so a formatter cannot quietly delete the two things that identify a session.
            self.assertEqual(metadata[6:],
                             ["[%s] codex" % minted_tag(sid), "--title",
                              "[%s] codex Session summary" % minted_tag(sid)])

            formatter.write_text("#!/usr/bin/env python3\nprint('{')\n")
            formatter.chmod(formatter.stat().st_mode | stat.S_IXUSR)
            _, rows = self.project(root, sid, title="Fallback", formatter=formatter)
            metadata = next(row for row in rows if row[1] == "report-metadata")
            self.assertEqual(metadata[6:],
                             ["[%s] codex" % minted_tag(sid), "--title",
                              "[%s] codex Fallback" % minted_tag(sid)])

            formatter.write_text("#!/usr/bin/env python3\nimport time;time.sleep(.5)\n")
            formatter.chmod(formatter.stat().st_mode | stat.S_IXUSR)
            _, rows = self.project(root, sid, title="Timeout", formatter=formatter)
            metadata = next(row for row in rows if row[1] == "report-metadata")
            self.assertEqual(metadata[6:],
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
                expected = current_session_subject(None, title)
                self.assertEqual(expected, title)
                if harness == "claude":
                    self.successful_title(root, sid, expected)
                    native_title = "Automatic stdin title"
                    statusline_output = self.statusline(root, sid, native_title).stdout
                    self.assertIn(expected, statusline_output)
                    self.assertNotIn(native_title, statusline_output)
                else:
                    _, rows = self.project(root, sid, title=title)
                    metadata = next(row for row in rows if row[1] == "report-metadata")
                    self.assertEqual(metadata[-1], "[%s] codex %s" % (minted_tag(sid), title))
                self.assertEqual(render._session_name(SimpleNamespace(
                    runtime_name=None, title=title)), expected)

    def test_pane_header_order_is_number_then_steward_then_harness(self):
        """User-fixed format `[번호] (⚑) 하네스 요약` (2026-09-09; ⚑ moved before the harness
        2026-10-14), one shape everywhere."""
        for harness in ("claude", "codex", "opencode"):
            self.assertEqual(compose(harness, "s", tag="3a", steward=False, title="사이클"),
                             ("[3a] %s" % harness, "사이클"))
            self.assertEqual(compose(harness, "s", tag="b0", steward=True, title="감독")[0],
                             "[b0] ⚑ %s" % harness)
        # No tag resolves: the badge slot is dropped rather than shown empty or faked.
        self.assertEqual(compose("claude", "s", tag=None, steward=False, title="t")[0],
                         "claude")
        # Budgets are herdr's; a long title is clipped, never wrapped into the agent cell.
        agent, title = compose("codex", "s", tag="3a", steward=True, title="가" * 80)
        self.assertEqual(agent, "[3a] ⚑ codex")
        self.assertLess(len(title), 80)

    def test_marker_free_steward_lookup_skips_live_session_snapshot(self):
        from fleet.collectors import steward
        from tools.fleet.herdr_projection import is_steward

        with unittest.mock.patch.object(steward, "read_markers", return_value={}) as read_markers, \
             unittest.mock.patch.object(steward, "_projection_sessions") as projection_sessions:
            self.assertFalse(is_steward("claude", "session-without-role-evidence"))
        read_markers.assert_called_once_with()
        projection_sessions.assert_not_called()

    def test_claude_display_hook_publishes_metadata_without_duplicate_session_lifecycle(self):
        """The native SessionStart integration owns lifecycle; Hearting paints metadata."""
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

    def test_codex_prompt_hook_never_reports_a_fake_session_and_projects_only_current_metadata(self):
        """The 2026-09-24 leak: `portable-guards.test.sh` fed `directpromptsid` to the Codex
        prompt hook while inheriting the pane's HERDR_*, and a live header read
        `[0d] codex`. The hook's process is not that session's runtime, so nothing is sent
        for a fake session. A session report requires this process to hold the exact native
        rollout and pass its footer proof; metadata requires the same current thread. Asserted
        in every case, whatever
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
                    rows = [json.loads(line) for line in log.read_text().splitlines()]
                    self.assertEqual([row[1] for row in rows],
                                     ["report-agent-session", "report-metadata"], case)
                    self.assertEqual(rows[0], [
                        "pane", "report-agent-session", "wB:pN", "--source", "herdr:codex",
                        "--agent", "codex", "--agent-session-id", CODEX_SID,
                    ], case)
                    metadata = [row for row in rows if row[1] == "report-metadata"]
                    self.assertEqual(len(metadata), 1, case)
                    self.assertEqual(metadata[0][3:6],
                                     ["--source", "herdr:codex", "--display-agent"], case)
                    self.assertEqual(metadata[0][6], "[cd] codex", case)
                else:
                    self.assertEqual(log.read_text(), "", case)

    def direct_codex(self, root, bindir, sids, created_offset=0):
        """A direct Codex TUI holding NO rollout fd (2026-08-10): a process named ``codex``
        whose prelude writes each root rollout, stamped with the current time, just after
        it starts — as Codex does at thread start — and closes it again. Only the board's
        process-start match can prove it. The fd-held fixture (`own_codex_thread`) writes
        no ``timestamp``, so the start match can never flip the table test's cases."""
        interpreter = bindir / "codex"
        if not os.path.lexists(interpreter):
            os.symlink(sys.executable, interpreter)
        base = root / "codex" / "sessions" / "2026" / "09" / "26"
        base.mkdir(parents=True, exist_ok=True)
        rollouts = [(str(base / ("rollout-2026-09-26T00-00-00-%s.jsonl" % sid)),
                     json.dumps({"type": "session_meta", "payload": {
                         "id": sid, "cwd": os.path.realpath(root), "timestamp": "@NOW@"}}))
                    for sid in sids]
        prelude = ("import datetime as _d;_now=(_d.datetime.now(_d.timezone.utc)"
                   "+_d.timedelta(seconds=%d)).isoformat()\n" % created_offset +
                   "for _p,_m in %r:\n    open(_p,'w').write(_m.replace('@NOW@',_now)+'\\n')\n"
                   % (rollouts,))
        return str(interpreter), prelude

    def direct_codex_reports(self, root, sids, payload_sid, sibling=False, created_offset=0):
        """Run the Codex prompt hook under `direct_codex`; True when herdr was reached."""
        hook = ROOT / "adapters/codex/hooks/userprompt-lifecycle.py"
        bindir, log = self.stub(root)
        interpreter, prelude = self.direct_codex(root, bindir, sids, created_offset)
        env = self.env(root)
        env.update({"PATH": str(bindir) + os.pathsep + env["PATH"],
                    "HERDR_ENV": "1", "HERDR_PANE_ID": "wB:pN",
                    "HERDR_SOCKET_PATH": str(root / "live.sock"),
                    "HERDR_LOG": str(log), "HERDR_MODE": "ok", "HERDR_EXIT": "0"})
        # The footer names one thread only for one rollout and no competing sibling; an
        # ambiguous pair leaves it unreadable, which proves nothing.
        footer = sids[0] if len(sids) == 1 and not sibling else ""
        runner = prelude + ("import os,subprocess,sys;os.environ['HERDR_CURRENT_PID']=str(os.getpid());"
                            "os.environ['HERDR_CURRENT_FOOTER']=%r;"
                            "subprocess.run([%r,%r],input=sys.argv[1],text=True,"
                            "capture_output=True,timeout=60)"
                            % (footer, sys.executable, str(hook)))
        payload = json.dumps({"prompt": "deterministic recall", "session_id": payload_sid,
                              "turn_id": "directturnid", "cwd": ""})
        other = None
        try:
            if sibling:
                # A second direct TUI in the same cwd, started inside the match window.
                other = subprocess.Popen([interpreter, "-c", "import time;time.sleep(90)"],
                                         cwd=str(root), env=env)
                for _ in range(100):
                    if Path("/proc/%d/comm" % other.pid).read_text().strip() == "codex":
                        break
                    time.sleep(0.02)
            log.write_text("")
            subprocess.run([interpreter, "-c", runner, payload], cwd=str(root), env=env,
                           capture_output=True, timeout=90, check=True)
        finally:
            if other is not None:
                other.kill()
                other.wait()
        return "report-agent-session" in log.read_text()





    def test_a_selection_change_during_the_report_window_suppresses_both_reports(self):
        from fleet import herdr_projection as hp
        decisions = iter([True, False])
        sent = []
        with unittest.mock.patch.object(hp.shutil, "which", return_value="/usr/bin/herdr"), \
             unittest.mock.patch.object(hp, "may_report",
                side_effect=lambda *a, **k: next(decisions)), \
             unittest.mock.patch.object(hp, "_report", side_effect=lambda *a, **k: sent.append(a)), \
             unittest.mock.patch.object(hp, "runtime_identity", return_value=("codex", None)):
            observation = {}
            self.assertTrue(hp.project("codex", CODEX_SID, pane_id="pane-9", worker=False,
                                       report_session=False, observation=observation))
        self.assertEqual(sent, [])
        self.assertEqual(observation["reason"], "guard-refused-before-report")















    def test_fdless_embedded_tui_without_daemon_loader_stays_unknown(self):
        from fleet.collectors import codex, procscan
        sid = "0f5d1a7e-5b1c-4d2e-9f3a-2b6c8d0e1f47"
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            bindir, _log = self.stub(root)
            interpreter, prelude = self.direct_codex(root, bindir, [sid])
            proc = subprocess.Popen([interpreter, "-c", prelude + "import time;time.sleep(90)"],
                                    cwd=str(root), env=self.env(root))
            try:
                rollout = next((root / "codex" / "sessions").rglob("*.jsonl"), None)
                for _ in range(200):
                    if rollout is not None and rollout.stat().st_size:
                        break
                    time.sleep(0.02)
                    rollout = next((root / "codex" / "sessions").rglob("*.jsonl"), None)
                with unittest.mock.patch.dict(os.environ, {"CODEX_HOME": str(root / "codex")}):
                    codex._INDEX.update(ts=0.0, map=None)
                    tick = codex.prepare_tick(procscan.scan(harness_filter={"codex"}))
                    self.assertIsNone(tick.proc_paths.get(proc.pid))
                    self.assertIsNone(codex.session_id_of_process(proc.pid))
            finally:
                proc.kill()
                proc.wait()
                codex._INDEX.update(ts=0.0, map=None)

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
            self.successful_title(root, sid, "Real title")
            out = strip(self.statusline(root, sid, "Automatic stdin title").stdout)
            segment = next(s for s in out.split("│") if "Real title" in s)
            self.assertEqual(segment.strip(), "Real title")   # title only, no `[46]` badge
            # A name that is only the folder is not a title: `📁 <dir>` already says it.
            self.successful_title(root, sid, root.name)
            bare = strip(self.statusline(root, sid, "Automatic stdin title").stdout)
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
        self.assertTrue(header_title("[94] ⚑ claude", "v6 Release").startswith(
            "[94] ⚑ claude "))

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
        from tools.fleet import herdr_projection
        with unittest.mock.patch.object(herdr_projection.shutil, "which", return_value="herdr"), \
             unittest.mock.patch.object(herdr_projection, "session_title", return_value="summary"), \
             unittest.mock.patch.object(herdr_projection, "_formatter_overrides", return_value=(None, None)), \
             unittest.mock.patch.object(herdr_projection, "compose", return_value=("codex", "summary")), \
             unittest.mock.patch.object(herdr_projection, "_send_projection", autospec=True) as send:
            herdr_projection._report("codex", "sid", "pane", False)
        self.assertEqual(send.call_count, 1)
        metadata = send.call_args.args[0]
        self.assertIn("--display-agent", metadata)
        self.assertIn("--title", metadata)

    def test_permission_denied_optional_report_does_not_raise_or_retry(self):
        """Pane metadata is optional: a herdr socket denial must not block the hook."""
        from tools.fleet import herdr_projection
        with unittest.mock.patch.object(herdr_projection.shutil, "which", return_value="/usr/bin/herdr"), \
             unittest.mock.patch.object(herdr_projection, "session_title", return_value="summary"), \
             unittest.mock.patch.object(herdr_projection, "_formatter_overrides", return_value=(None, None)), \
             unittest.mock.patch.object(herdr_projection, "compose", return_value=("codex", "summary")), \
             unittest.mock.patch.object(herdr_projection.subprocess, "run",
                                        side_effect=PermissionError("Operation not permitted")) as run:
            herdr_projection._report("codex", "sid", "pane-7", True)
        self.assertEqual(run.call_count, 2)


class ProjectionObservationTest(unittest.TestCase):
    def test_public_bool_guard_and_quiet_other_cli_contracts(self):
        from contextlib import redirect_stdout
        from io import StringIO
        from tools.fleet import herdr_projection as hp
        with unittest.mock.patch.object(hp.shutil, "which", return_value="/fixture/herdr"), \
             unittest.mock.patch.object(hp, "may_report", return_value=False), \
             unittest.mock.patch.object(hp, "runtime_identity", return_value=(None, None)), \
             unittest.mock.patch.object(hp, "_report") as report:
            observation = {}
            self.assertIs(hp.project("opencode", "callback", pane_id="fixture", observation=observation), True)
            self.assertEqual(observation, {"schema": "hearting-pane-observation-v1", "reason": "guard-refused",
                                          "session_report": "not-attempted", "metadata_report": "not-attempted"})
            for harness in ("opencode", "claude", "codex"):
                output = StringIO()
                with redirect_stdout(output):
                    self.assertEqual(hp.main(["--harness", harness, "--session-id", "callback", "--pane", "fixture"]), 0)
                if harness == "opencode":
                    self.assertEqual(json.loads(output.getvalue()), observation)
                    self.assertEqual(len(output.getvalue().splitlines()), 1)
                    self.assertLess(len(output.getvalue().encode()), 512)
                else:
                    self.assertEqual(output.getvalue(), "")
            report.assert_not_called()
            output = StringIO()
            with redirect_stdout(output):
                self.assertEqual(hp.main(["--harness", "opencode", "--session-id", "callback", "--may-report"]), 1)
            self.assertEqual(output.getvalue(), "")
        with unittest.mock.patch.object(hp, "compose", return_value=("shown", "title")):
            output = StringIO()
            with redirect_stdout(output):
                self.assertEqual(hp.main(["--harness", "opencode", "--session-id", "callback", "--print"]), 0)
            self.assertEqual(json.loads(output.getvalue()), {"display_agent": "shown", "title": "title"})

    def test_report_observation_preserves_command_timeout_error_and_no_retry(self):
        from tools.fleet import herdr_projection as hp
        cases = [(SimpleNamespace(returncode=0), "exit0", 0),
                 (SimpleNamespace(returncode=7), "nonzero", 7),
                 (subprocess.TimeoutExpired("private-command", .5), "timeout", None),
                 (PermissionError("private-error"), "spawn-error", None)]
        for outcome, status, rc in cases:
            with self.subTest(status=status), \
                 unittest.mock.patch.object(hp.shutil, "which", return_value="/fixture/herdr"), \
                 unittest.mock.patch.object(hp, "session_title", return_value="private-title"), \
                 unittest.mock.patch.object(hp, "_formatter_overrides", return_value=(None, None)), \
                 unittest.mock.patch.object(hp, "compose", return_value=("opencode", "private-title")), \
                 unittest.mock.patch.object(hp.subprocess, "run", side_effect=[outcome, outcome]) as run:
                observation = {}
                hp._report("opencode", "exact", "fixture-pane", True, observation=observation)
                self.assertEqual(run.call_count, 2)
                self.assertEqual(observation["reason"], "report-attempts-finished")
                self.assertEqual(observation["session_report"], status)
                self.assertEqual(observation["metadata_report"], status)
                self.assertEqual(observation.get("session_report_rc"), rc)
                self.assertEqual(observation.get("metadata_report_rc"), rc)
                self.assertNotIn("private", json.dumps(observation))
                self.assertNotIn("received", observation)
                self.assertTrue(all(call.kwargs["stdout"] == subprocess.DEVNULL and
                                    call.kwargs["stderr"] == subprocess.DEVNULL for call in run.call_args_list))

    def test_known_old_runtime_sid_can_wait_for_the_new_foreground_footer(self):
        from tools.fleet import herdr_projection as hp
        with unittest.mock.patch.object(hp.shutil, "which", return_value="herdr"), \
             unittest.mock.patch.object(hp, "may_report", return_value=False), \
             unittest.mock.patch.object(hp, "runtime_identity", return_value=("codex", "old")), \
             unittest.mock.patch.object(hp, "_nearest_codex_pid", return_value=42), \
             unittest.mock.patch.object(hp, "verified_pane", return_value="pane"), \
             unittest.mock.patch.object(hp, "_defer_until_proven") as defer, \
             unittest.mock.patch.object(hp, "_report") as report:
            hp.project("codex", "new", pane_id="pane", worker=False,
                       session_seq=7, session_start_source="clear")
            defer.assert_called_once_with("new", True, session_seq=7,
                                          session_start_source="clear")
            report.assert_not_called()

    def test_a_switch_during_metadata_formatting_suppresses_the_old_metadata(self):
        from tools.fleet import herdr_projection as hp
        with unittest.mock.patch.object(hp.shutil, "which", return_value="herdr"), \
             unittest.mock.patch.object(hp, "_metadata_command", return_value=["metadata"]), \
             unittest.mock.patch.object(hp, "_send_projection") as send:
            observation = {}
            proof = iter((True, False))
            hp._report("codex", "old", "pane", True, observation=observation,
                       _proof=lambda: next(proof))
            self.assertEqual(send.call_count, 1)
            self.assertEqual(send.call_args.args[0][2], "report-agent-session")
            self.assertEqual(observation["reason"], "guard-refused-before-metadata")

    def test_without_a_herdr_pane_every_harness_takes_its_tmux_pane_as_the_hint(self):
        from tools.fleet import herdr_projection as hp
        env = {"HERDR_PANE_ID": "", "TMUX": "/fixture/tmux.sock,1,0", "TMUX_PANE": "%7"}
        for harness in ("claude", "codex", "opencode"):
            with self.subTest(harness=harness), unittest.mock.patch.dict(os.environ, env), \
                 unittest.mock.patch.object(hp, "_in_shared_codex_service", return_value=False), \
                 unittest.mock.patch.object(hp, "_project_tmux", return_value=True) as tmux:
                self.assertIs(hp.project(harness, "exact", report_session=False), True)
                # The hint carries its server, as every tmux pane address does.
                self.assertEqual(tmux.call_args.args[:3], (harness, "exact", "%7@/fixture/tmux.sock"))
        # A shared Codex service runs hooks in some other pane's environment: no hint.
        with unittest.mock.patch.dict(os.environ, env), \
             unittest.mock.patch.object(hp, "_in_shared_codex_service", return_value=True), \
             unittest.mock.patch.object(hp.shutil, "which", return_value=None), \
             unittest.mock.patch.object(hp, "_project_tmux") as tmux:
            self.assertIs(hp.project("codex", "exact"), True)
            tmux.assert_not_called()
        # A Herdr pane stays Herdr; an explicit pane is never replaced.
        for kwargs, extra in (({}, {"HERDR_PANE_ID": "w1:p2"}), ({"pane_id": "w1:p3"}, {})):
            with unittest.mock.patch.dict(os.environ, dict(env, **extra)), \
                 unittest.mock.patch.object(hp.shutil, "which", return_value=None), \
                 unittest.mock.patch.object(hp, "_project_tmux") as tmux:
                self.assertIs(hp.project("opencode", "exact", **kwargs), True)
                tmux.assert_not_called()

    def test_unavailable_and_no_report_session_observation_never_promote_identity(self):
        from tools.fleet import herdr_projection as hp
        with unittest.mock.patch.object(hp.shutil, "which", return_value=None), \
             unittest.mock.patch.object(hp, "_report") as report:
            observation = {}
            self.assertIs(hp.project("opencode", "exact", observation=observation), True)
            self.assertEqual(observation["reason"], "herdr-unavailable")
            report.assert_not_called()
        with unittest.mock.patch.dict(os.environ, {"HERDR_PANE_ID": ""}), \
             unittest.mock.patch.object(hp.shutil, "which", return_value="/fixture/herdr"), \
             unittest.mock.patch.object(hp, "_report") as report:
            observation = {}
            self.assertIs(hp.project("opencode", "exact", observation=observation), True)
            self.assertEqual(observation["reason"], "pane-unavailable")
            report.assert_not_called()
        with unittest.mock.patch.object(hp.shutil, "which", return_value="/fixture/herdr"), \
             unittest.mock.patch.object(hp, "session_title", return_value=""), \
             unittest.mock.patch.object(hp, "_formatter_overrides", return_value=(None, None)), \
             unittest.mock.patch.object(hp, "compose", return_value=("opencode", "")), \
             unittest.mock.patch.object(hp.subprocess, "run", return_value=SimpleNamespace(returncode=0)) as run:
            observation = {}
            hp._report("opencode", "exact", "fixture", False, observation=observation)
            self.assertEqual(observation["session_report"], "skipped")
            self.assertEqual(observation["metadata_report"], "exit0")
            self.assertEqual(run.call_count, 1)
            self.assertEqual(run.call_args.args[0][2], "report-metadata")

    def test_native_opencode_lifecycle_fields_keep_exact_argv_and_metadata(self):
        from tools.fleet import herdr_projection as hp
        prefix = ["/fixture/herdr", "pane", "report-agent-session", "fixture-pane", "--source", "herdr:opencode",
                  "--agent", "opencode", "--agent-session-id", "ses_A"]
        cases = [(1000001, "startup", True, prefix + ["--seq", "1000001", "--session-start-source", "startup"]),
                 (1000002, None, True, prefix + ["--seq", "1000002"]),
                 (None, None, True, prefix), (1000003, "startup", False, None)]
        for seq, start, report_session, expected in cases:
            with self.subTest(seq=seq, start=start, report=report_session), \
                 unittest.mock.patch.object(hp.shutil, "which", return_value="/fixture/herdr"), \
                 unittest.mock.patch.object(hp, "session_title", return_value="title"), \
                 unittest.mock.patch.object(hp, "_formatter_overrides", return_value=(None, None)), \
                 unittest.mock.patch.object(hp, "compose", return_value=("opencode", "title")), \
                 unittest.mock.patch.object(hp.subprocess, "run", return_value=SimpleNamespace(returncode=0)) as run:
                observation = {}
                hp._report("opencode", "ses_A", "fixture-pane", report_session, observation=observation,
                           session_seq=seq, session_start_source=start)
                commands = [call.args[0] for call in run.call_args_list]
                self.assertEqual(commands[:-1], [expected] if expected else [])
                self.assertEqual(commands[-1], ["/fixture/herdr", "pane", "report-metadata", "fixture-pane",
                    "--source", "herdr:opencode", "--display-agent", "opencode", "--title", "opencode title"])
                self.assertEqual(observation["session_report"], "exit0" if report_session else "skipped")

    def test_absolute_native_resume_argv_is_canonicalized_only_at_wire_boundary(self):
        from tools.fleet import herdr_projection as hp
        cases = json.loads((ROOT / "tools/fleet/tests/native_resume_cases.json").read_text())
        for case in cases:
            with self.subTest(name=case["name"]):
                lifecycle = hp._session_lifecycle(case["harness"], 1000001, "clear",
                                                  case["argv"], session_id=case["sid"])
                if case["expected"] is None:
                    self.assertNotIn("resume_argv", lifecycle)
                else:
                    self.assertEqual(lifecycle["resume_argv"], case["expected"])
        self.assertNotIn("resume_argv", hp._session_lifecycle(
            "claude", 1000001, "resume", ["/usr/bin/env", "claude", "--resume", "ses_A"],
            session_id="ses_A"))
        self.assertEqual(hp._session_lifecycle(
            "claude", 1000001, "resume", ["/opt/claude", "--resume", "other-session"],
            session_id="ses_A")["resume_argv"], ["claude", "--resume", "ses_A"])

    def test_python_opencode_lifecycle_keeps_only_the_process_bound_selection_argv(self):
        from fleet import process_identity
        from fleet import herdr_projection as hp
        from fleet.collectors import opencode
        sid = "ses_A"
        argv = ["/home/user/.local/bin/opencode", "--session", sid, "--model", "fixture"]
        selection = {"value": {"sessionID": sid, "sessionSeq": 1000001,
                               "sessionStartSource": "clear"}}
        launch = {"pid": 4321, "proc_start": "123", "argv": argv}
        with unittest.mock.patch.object(process_identity, "current_launch_context", return_value=launch), \
                unittest.mock.patch.object(opencode, "_tui_selection_record", return_value=selection):
            lifecycle = hp._native_opencode_lifecycle(sid)
        self.assertEqual(lifecycle["resume_argv"], ["opencode", "--session", sid, "--model", "fixture"])
        self.assertEqual(lifecycle["session_seq"], 1000001)

        selection["value"]["sessionID"] = "ses_other"
        with unittest.mock.patch.object(process_identity, "current_launch_context", return_value=launch), \
                unittest.mock.patch.object(opencode, "_tui_selection_record", return_value=selection):
            self.assertEqual(hp._native_opencode_lifecycle(sid), {})

    def test_invalid_bootstrap_fields_never_promote_or_bypass_original_guard(self):
        from tools.fleet import herdr_projection as hp
        cases = [(True, "startup"), (0, "startup"), (-1, None), (9007199254740992, None),
                 ("100", "startup"), (100, "new"), (None, "startup")]
        for seq, start in cases:
            with self.subTest(seq=seq, start=start), \
                 unittest.mock.patch.object(hp.shutil, "which", return_value="/fixture/herdr"), \
                 unittest.mock.patch.object(hp, "may_report", return_value=True), \
                 unittest.mock.patch.object(hp, "_report") as report:
                self.assertIs(hp.project("opencode", "ses_A", pane_id="fixture-pane",
                                        session_seq=seq, session_start_source=start), True)
                report.assert_called_once_with("opencode", "ses_A", "fixture-pane", False)
        with unittest.mock.patch.object(hp.shutil, "which", return_value="/fixture/herdr"), \
             unittest.mock.patch.object(hp, "may_report", return_value=False), \
             unittest.mock.patch.object(hp, "_report") as report:
            self.assertIs(hp.project("opencode", "ses_A", pane_id="fixture-pane",
                                    session_seq=100, session_start_source="startup"), True)
            report.assert_not_called()

    def test_recorded_start_lifecycle_converts_ledger_microseconds_per_harness(self):
        from tools.fleet import herdr_projection as hp
        sys.path.insert(0, str(ROOT / "utilities"))
        import session_tidy
        recorded = {"session_start_source": "clear", "session_seq": 1793262080500000}
        for harness, expected in (("codex", 1793262080500000000), ("claude", 1793262080500000000),
                                  ("opencode", 1793262080500000)):
            with self.subTest(harness=harness), \
                 unittest.mock.patch.object(session_tidy, "session_start_context",
                                            return_value=dict(recorded)) as read:
                lifecycle = hp._recorded_start_lifecycle(harness, "sid", "w:p", socket_path="/s.sock")
                self.assertEqual(lifecycle, {"session_seq": expected, "session_start_source": "clear"})
                read.assert_called_once_with(
                    harness, "sid", "w:p",
                    endpoint=session_tidy._endpoint_scope({"HERDR_SOCKET_PATH": "/s.sock"}))
        with unittest.mock.patch.object(session_tidy, "session_start_context",
                                        return_value=dict(recorded)) as read:
            hp._recorded_start_lifecycle("codex", "sid", "w:p")
            read.assert_called_once_with("codex", "sid", "w:p", endpoint=None)
        # a recorded source with no recorded seq stays without one: no seq is ever made here
        for context, expected in (({}, {}),
                                  ({"session_start_source": "clear"}, {"session_start_source": "clear"})):
            with self.subTest(context=context), \
                 unittest.mock.patch.object(session_tidy, "session_start_context", return_value=context), \
                 unittest.mock.patch.object(hp.time, "time_ns", side_effect=AssertionError("no new seq")):
                self.assertEqual(hp._recorded_start_lifecycle("codex", "sid", "w:p", socket_path=""),
                                 expected)

    def test_direct_codex_projection_sends_recorded_start_in_nanoseconds_and_keeps_explicit_seq(self):
        from tools.fleet import herdr_projection as hp
        sys.path.insert(0, str(ROOT / "utilities"))
        import session_tidy
        from fleet import process_identity
        recorded = {"session_start_source": "clear", "session_seq": 1793262080500000}
        common = (unittest.mock.patch.object(hp.shutil, "which", return_value="/fixture/herdr"),
                  unittest.mock.patch.object(hp, "may_report", return_value=True),
                  unittest.mock.patch.object(process_identity, "current_launch_context", return_value=None))
        with common[0], common[1], common[2], \
                unittest.mock.patch.object(session_tidy, "session_start_context",
                                           return_value=dict(recorded)), \
                unittest.mock.patch.object(hp, "_report") as report:
            hp.project("codex", "sid", pane_id="w:p", worker=False)
        self.assertEqual(report.call_args.kwargs["session_seq"], 1793262080500000000)
        self.assertEqual(report.call_args.kwargs["session_start_source"], "clear")

        with common[0], common[1], common[2], \
                unittest.mock.patch.object(session_tidy, "session_start_context") as read, \
                unittest.mock.patch.object(hp, "_report") as report:
            hp.project("codex", "sid", pane_id="w:p", worker=False,
                       session_seq=1793262080600000000, session_start_source="clear")
        read.assert_not_called()
        self.assertEqual(report.call_args.kwargs["session_seq"], 1793262080600000000)

        with common[0], common[1], common[2], \
                unittest.mock.patch.object(session_tidy, "session_start_context",
                                           return_value=dict(recorded)), \
                unittest.mock.patch.object(hp, "_native_opencode_lifecycle", return_value={}), \
                unittest.mock.patch.object(hp, "_report") as report:
            hp.project("opencode", "ses_A", pane_id="w:p", worker=False)
        self.assertEqual(report.call_args.kwargs["session_seq"], 1793262080500000)

    def test_normal_opencode_cli_transports_lifecycle_without_changing_other_harnesses(self):
        from contextlib import redirect_stdout
        from io import StringIO
        from tools.fleet import herdr_projection as hp
        with unittest.mock.patch.object(hp, "project") as project:
            output = StringIO()
            with redirect_stdout(output):
                self.assertEqual(hp.main(["--harness", "opencode", "--session-id", "ses_A", "--pane", "fixture-pane",
                                          "--seq", "1000001", "--session-start-source", "startup"]), 0)
            project.assert_called_once_with("opencode", "ses_A", pane_id="fixture-pane", report_session=True,
                                            observation={}, session_seq=1000001, session_start_source="startup")
            self.assertEqual(output.getvalue(), "{}\n")
        for harness in ("codex", "claude"):
            with self.subTest(harness=harness), unittest.mock.patch.object(hp, "project") as project:
                output = StringIO()
                with redirect_stdout(output):
                    self.assertEqual(hp.main(["--harness", harness, "--session-id", "actual"]), 0)
                project.assert_called_once_with(harness, "actual", pane_id=None, report_session=True, observation=None)
                self.assertEqual(output.getvalue(), "")



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

    def test_native_transcript_title_cannot_replace_a_failed_fleet_title(self):
        # The real case: the title worker ran and failed, leaving `title: ""`.
        self._write_sidecar("")
        self._write_transcript("r1-model S8 재진입 버그 수정")
        self.assertEqual(self._title(), "")

    def test_a_fresh_sidecar_still_outranks_the_transcript(self):
        # Order matters: the sidecar is the worker's considered summary, the ai-title is
        # whatever the runtime named the session first.
        self._write_transcript("older transcript title")
        self._write_sidecar("v6 Command Model Release")
        self.assertEqual(self._title(), "v6 Command Model Release")

    def test_last_successful_sidecar_survives_without_native_title_substitution(self):
        # The board drops a sidecar this old; the header keeps it rather than going blank,
        # but only once the rung the board WOULD have used has been tried.
        self._write_sidecar("aged summary", age_sec=86400)
        self._write_transcript("current transcript title")
        self.assertEqual(self._title(), "aged summary")
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
