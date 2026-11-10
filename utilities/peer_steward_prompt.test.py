#!/usr/bin/env python3
"""peer-steward kill, prompt resolution, notice carrier, obligation and clear/continue tests.

Split from peer_steward.test.py, which keeps the shared fixtures, helpers and
screen constants, so the parts run on separate CI shards. Test class and method
names are unchanged.
"""
import importlib.util
import sys
import unittest
from pathlib import Path

# A suite that borrows classes from both parts gets one fixture instance, so
# both see the same loaded peer-steward module.
FIXTURE = sys.modules.get("peer_steward_test_fixture")
if FIXTURE is None:
    _FIXTURE_SPEC = importlib.util.spec_from_file_location(
        "peer_steward_test_fixture", Path(__file__).resolve().with_name("peer_steward.test.py"))
    FIXTURE = importlib.util.module_from_spec(_FIXTURE_SPEC)
    sys.modules[_FIXTURE_SPEC.name] = FIXTURE
    _FIXTURE_SPEC.loader.exec_module(FIXTURE)
# The classes below use the fixture module's imports, helpers and constants by
# their bare names, as they did before the split. Its own test classes are not
# imported, so they run only in peer_steward.test.py.
globals().update({name: value for name, value in vars(FIXTURE).items()
                  if not name.startswith("__")
                  and not (isinstance(value, type) and issubclass(value, unittest.TestCase))})


class KillFixtureTest(_WatchMixin, unittest.TestCase):
    """A56-7 — the smallest falsifying test for the whole direction."""

    def _spawn_caller(self, subcommand):
        script = (
            "import subprocess,sys;"
            f"p=subprocess.run([sys.executable,{str(_HERE / 'peer-steward.py')!r},"
            f"{subcommand!r},'peer-a'],capture_output=True,text=True);"
            "open(%r,'w').write(p.stdout);" % str(self.tmp_root / "caller.out")
            + "import time;time.sleep(300)"
        )
        return subprocess.Popen([sys.executable, "-c", script], env=self._env("held"),
                                start_new_session=True)

    def test_watcher_survives_sigkill_of_the_callers_process_group(self):
        caller = self._spawn_caller("watch")
        # Read the watcher identity from disk, not from the caller's stdout:
        # a killed caller's stdout may never be flushed.
        deadline = time.monotonic() + 30
        arm = None
        while time.monotonic() < deadline:
            # Only "<watch_id>.json" is an arm record. The writer's atomic temp
            # file (".tmp-*.json") matches "*.json" too and vanishes at its
            # rename (FileNotFoundError on CI), and observation/receipt files
            # carry no watcher identity.
            arms = ([p for p in self.watch_root.glob("*.json")
                     if not p.name.startswith(".") and p.name.count(".") == 1]
                    if self.watch_root.is_dir() else [])
            if arms:
                candidate = json.loads(arms[0].read_text())
                # The accepted record exists before its observer is published.
                # Kill the caller only after that observer's identity is ready.
                watcher = candidate.get("watcher") or {}
                if watcher.get("pid") and watcher.get("pid_start"):
                    arm = candidate
                    break
            time.sleep(0.02)
        self.assertIsNotNone(arm, "watch never armed")
        pid, pid_start = arm["watcher"]["pid"], arm["watcher"]["pid_start"]

        os.killpg(os.getpgid(caller.pid), signal.SIGKILL)
        caller.wait(timeout=10)

        self.assertTrue(peer_steward._pid_identity_ok(pid, pid_start),
                        "the watcher must outlive its caller's process group")
        self._release()
        receipt = self._wait_for_receipt(arm["watch_id"])
        self.assertEqual(receipt["state"], "idle")

    def _run_spawner(self, argv, after_spawn, env=None):
        """Run one peer-steward command with `after_spawn` right after its observer spawn.

        Spawners record the observer identity in the arm only after the spawn;
        `after_spawn` (source using `process`) acts in exactly that gap.
        """
        source = (
            "import importlib.util, os, signal, subprocess, sys\n"
            f"sys.path.insert(0, {str(_HERE)!r})\n"
            "spec = importlib.util.spec_from_file_location(\n"
            f"    'peer_steward_spawner', {str(_HERE / 'peer-steward.py')!r})\n"
            "module = importlib.util.module_from_spec(spec)\n"
            "spec.loader.exec_module(module)\n"
            "real_popen = subprocess.Popen\n"
            "def popen(command, *args, **kwargs):\n"
            "    process = real_popen(command, *args, **kwargs)\n"
            "    if '__watch-run' in command:\n"
            f"        {after_spawn}\n"
            "    return process\n"
            "subprocess.Popen = popen\n"
            f"sys.exit(module.main({list(argv)!r}))\n"
        )
        return subprocess.run([sys.executable, "-c", source], env=env or self._env("held"),
                              capture_output=True, text=True, timeout=60)

    def _run_killed_after_spawn(self, argv):
        spawner = self._run_spawner(argv, "os.kill(os.getpid(), signal.SIGKILL)")
        self.assertEqual(spawner.returncode, -signal.SIGKILL, spawner.stdout + spawner.stderr)

    def _wait_for_named_observer(self, arm_path):
        # The observer holding the watch lock must be named by its arm, or the
        # runner cannot wake it and no later pass can replace it.
        deadline = time.monotonic() + 10
        arm = json.loads(arm_path.read_text())
        while (not peer_steward._alive(arm, self.watch_root)
               and time.monotonic() < deadline):
            time.sleep(0.02)
            arm = json.loads(arm_path.read_text())
        self.assertTrue(peer_steward._alive(arm, self.watch_root), arm)
        return arm

    def _armed_with_dead_observer(self):
        """A watch whose first observer started its native wait and was killed."""
        armed = self._fields(self._run("watch", "peer-a", env=self._env("held")).stdout)
        self._wait_for_lock(armed["watch_id"])
        self._wait_for_wait_calls(1)
        os.kill(int(armed["pid"]), signal.SIGKILL)
        deadline = time.monotonic() + 10
        while (peer_steward._pid_identity_ok(int(armed["pid"]), armed["pid_start"])
               and time.monotonic() < deadline):
            time.sleep(0.02)
        self.assertFalse(peer_steward._pid_identity_ok(int(armed["pid"]), armed["pid_start"]))
        return armed

    def test_watcher_names_itself_when_its_caller_dies_right_after_the_spawn(self):
        self._run_killed_after_spawn(["watch", "peer-a"])
        arms = self._arm_records()
        self.assertEqual(len(arms), 1, arms)
        arm = self._wait_for_named_observer(arms[0])

        self._release()
        receipt = self._wait_for_receipt(arm["watch_id"])
        self.assertEqual(receipt["state"], "idle")
        self.assertEqual(receipt["watcher"], arm["watcher"])

    def test_watcher_that_cannot_name_itself_exits_so_a_respawn_can(self):
        # The spawner dies before naming its observer, and the observer's own
        # arm write fails (ENOSPC). The observer must end and release the
        # watch lock: an unnamed lock holder can be neither woken nor replaced.
        site = self.tmp_root / "fixture-site"
        site.mkdir()
        (site / "sitecustomize.py").write_text(
            "import errno, os, re\n"
            "if b'__watch-run' in open('/proc/self/cmdline', 'rb').read().split(b'\\0'):\n"
            "    _replace = os.replace\n"
            "    def replace(src, dst, *args, **kwargs):\n"
            "        if re.fullmatch(r'[0-9a-f]{16}\\.json', os.path.basename(str(dst))):\n"
            "            raise OSError(errno.ENOSPC, 'fixture: arm write refused')\n"
            "        return _replace(src, dst, *args, **kwargs)\n"
            "    os.replace = replace\n", encoding="utf-8")
        env = self._env("held")
        env["PYTHONPATH"] = os.pathsep.join(
            item for item in (str(site), env.get("PYTHONPATH")) if item)
        spawner = self._run_spawner(["watch", "peer-a"], "os.kill(os.getpid(), signal.SIGKILL)",
                                    env=env)
        self.assertEqual(spawner.returncode, -signal.SIGKILL, spawner.stdout + spawner.stderr)
        arms = self._arm_records()
        self.assertEqual(len(arms), 1, arms)
        watch_id = json.loads(arms[0].read_text())["watch_id"]
        lock = self.watch_root / f"{watch_id}.lock"
        deadline = time.monotonic() + 10
        while peer_steward._lock_held(lock) and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertFalse(peer_steward._lock_held(lock),
                         "an observer that could not name itself kept the watch lock")
        self.assertEqual(json.loads(arms[0].read_text())["watcher"], {})

        rearmed = self._fields(self._run("rearm", watch_id, env=self._env("held")).stdout)
        self.assertEqual(rearmed.get("state"), "rearmed", rearmed)
        arm = self._wait_for_named_observer(arms[0])
        self._release()
        receipt = self._wait_for_receipt(watch_id)
        self.assertEqual(receipt["state"], "idle")
        self.assertEqual(receipt["watcher"], arm["watcher"])

    def test_reattached_watcher_names_itself_when_its_spawner_dies_right_after_the_spawn(self):
        # Here the arm still names the previous observer, which has exited.
        armed = self._armed_with_dead_observer()
        self._run_killed_after_spawn(["rearm", armed["watch_id"]])
        arm = self._wait_for_named_observer(self.watch_root / f"{armed['watch_id']}.json")
        self.assertNotEqual(arm["watcher"]["pid"], int(armed["pid"]))
        self.assertEqual(len(self._wait_calls()), 1,
                         "the reattached observer adopts the one native wait")
        # The generation was recorded before the spawn, so a later release
        # takeover still recognises this observer's exact argv.
        self.assertEqual((arm["observer_generation"], arm["rearm_count"]), (2, 1))
        cmdline = Path(f"/proc/{arm['watcher']['pid']}/cmdline").read_bytes()
        live = cmdline.decode().rstrip("\0").split("\0")[2:]
        duty, index = [], 0
        while index < len(live):
            if live[index] in {"--lock-fd", "--service-current"}:
                index += 2
                continue
            duty.append(live[index])
            index += 1
        self.assertIn(duty, peer_steward._watch_observer_takeover_argvs(armed["watch_id"], arm))

        self._release()
        receipt = self._wait_for_receipt(armed["watch_id"])
        self.assertEqual(receipt["state"], "idle")
        self.assertEqual(receipt["watcher"], arm["watcher"])

    def test_reattached_watcher_survives_a_wake_sent_right_after_its_spawn(self):
        # The first spawn holds SIGUSR1 blocked across the spawn so an early
        # wake waits for the handler; a reattach spawn must do the same.
        armed = self._armed_with_dead_observer()
        spawner = self._run_spawner(["rearm", armed["watch_id"]],
                                    "os.kill(process.pid, signal.SIGUSR1)")
        self.assertEqual(spawner.returncode, 0, spawner.stdout + spawner.stderr)
        watcher = json.loads(
            (self.watch_root / f"{armed['watch_id']}.json").read_text())["watcher"]
        self.assertNotEqual(watcher["pid"], int(armed["pid"]))
        usr1 = 1 << (signal.SIGUSR1 - 1)

        def signal_mask(field):
            for line in Path(f"/proc/{watcher['pid']}/status").read_text().splitlines():
                if line.startswith(field + ":"):
                    return int(line.split()[1], 16)
            return 0

        # Wait until its handler is installed and the signal unblocked, i.e.
        # until the early wake has been delivered one way or the other.
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                if signal_mask("SigCgt") & usr1 and not signal_mask("SigBlk") & usr1:
                    break
            except OSError:
                break
            time.sleep(0.02)
        self.assertTrue(peer_steward._pid_identity_ok(watcher["pid"], watcher["pid_start"]),
                        "the early wake killed the reattached observer")
        self._release()
        receipt = self._wait_for_receipt(armed["watch_id"])
        self.assertEqual(receipt["state"], "idle")

    def test_wait_by_contrast_loses_its_herdr_child(self):
        # The regression proof: the identical fixture against (9) `wait`.
        caller = self._spawn_caller("wait")
        deadline = time.monotonic() + 30
        child = None
        while time.monotonic() < deadline and child is None:
            for entry in Path("/proc").glob("[0-9]*"):
                try:
                    if entry.joinpath("comm").read_text().strip() != "herdr":
                        continue
                    stat = entry.joinpath("stat").read_text()
                    if str(caller.pid) in stat.split(")")[-1].split()[2:4]:
                        child = int(entry.name)
                        break
                except OSError:
                    continue
            if child is None:
                time.sleep(0.02)
        os.killpg(os.getpgid(caller.pid), signal.SIGKILL)
        caller.wait(timeout=10)
        if child is not None:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and Path(f"/proc/{child}").exists():
                time.sleep(0.02)
            self.assertFalse(Path(f"/proc/{child}").exists(),
                             "`wait`'s herdr child must die with the caller's group")
        self.assertEqual(list(self.watch_root.glob("*.receipt.json"))
                         if self.watch_root.is_dir() else [], [],
                         "`wait` leaves no receipt -- that is the defect (10) fixes")


class PeerReferenceAccessEvidenceTest(_TmpRootMixin, unittest.TestCase):
    def test_exact_local_codex_session_uses_declared_read_scope_evidence(self):
        target = {"harness": "codex", "session_id": "thread-exact", "pane": "w1:pX"}
        foreground = {"pid": os.getpid(), "start": "1", "argv": ["codex", "--resume"]}
        with mock.patch.object(peer_steward, "_retire_target",
                               return_value=("idle", target, 0, None)), \
             mock.patch.object(peer_steward, "_retire_foreground", return_value=foreground), \
             mock.patch.object(peer_steward.peer_message, "_reference_local_process_matches",
                               return_value=True):
            evidence = peer_steward._peer_reference_access_evidence(
                "codex-pane", "codex", "thread-exact", "w1:pX")
        self.assertEqual(evidence["recipient"], {
            "harness": "codex", "session_id": "thread-exact", "pane_id": "w1:pX"})
        self.assertEqual(evidence["local_process"]["pid"], os.getpid())
        self.assertEqual(evidence["local_process"]["pid_namespace"],
                         os.readlink(f"/proc/{os.getpid()}/ns/pid"))
        self.assertEqual(evidence["schema"], "peer-read-access-observation-v1")

    def test_proven_ordinary_codex_recipient_keeps_reference_presentation(self):
        target = {"harness": "codex", "session_id": "thread-exact", "pane": "w1:pX"}
        foreground = {"pid": os.getpid(), "start": "1", "argv": ["codex", "--resume"]}
        body = "[steer] normal supported delivery\n" + "evidence " * 300 + "\nlast condition: retain phase"
        with mock.patch.object(peer_steward, "_retire_target",
                               return_value=("idle", target, 0, None)), \
             mock.patch.object(peer_steward, "_retire_foreground", return_value=foreground), \
             mock.patch.object(peer_steward.peer_message, "_reference_local_process_matches",
                               return_value=True):
            evidence = peer_steward._peer_reference_access_evidence(
                "codex-pane", "codex", "thread-exact", "w1:pX")
            text, ref = peer_steward.peer_message.prepare_peer_message(
                body, {"harness": "claude", "session_id": "sender"},
                {"harness": "codex", "session_id": "thread-exact"},
                defer=True, reference_evidence=evidence)
        self.assertLess(len(text.encode("utf-8")), 1024)
        self.assertNotIn("last condition", text)
        self.assertEqual(peer_steward.peer_message.read_peer_payload(ref)["body"], body)
        self.assertEqual(peer_steward.peer_message._read_pending(ref)["to"]["session_id"], "thread-exact")

    def test_unknown_sidless_other_runtime_and_changed_session_fail_conservative(self):
        with mock.patch.object(peer_steward, "_retire_target") as target:
            self.assertIsNone(peer_steward._peer_reference_access_evidence(
                "fresh-pane", "codex", None, "w1:pX"))
            self.assertIsNone(peer_steward._peer_reference_access_evidence(
                "claude-pane", "claude", "claude-session", "w1:pX"))
        target.assert_not_called()

        replies = iter((
            ("idle", {"harness": "codex", "session_id": "old-thread", "pane": "w1:pX"}, 0, None),
            ("idle", {"harness": "codex", "session_id": "replacement", "pane": "w1:pX"}, 0, None),
        ))
        with mock.patch.object(peer_steward, "_retire_target", side_effect=lambda _target: next(replies)), \
             mock.patch.object(peer_steward, "_retire_foreground",
                               return_value={"pid": os.getpid(), "start": "1", "argv": ["codex"]}), \
             mock.patch.object(peer_steward.peer_message, "_reference_local_process_matches",
                               return_value=True):
            self.assertIsNone(peer_steward._peer_reference_access_evidence(
                "codex-pane", "codex", "old-thread", "w1:pX"))


class F100cPromptAndResolutionTest(_TmpRootMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        for key in ("AGENT_DISPATCH_CALLER_HARNESS", "AGENT_DISPATCH_CURRENT_HARNESS",
                    "CLAUDE_SESSION_ID", "CODEX_SESSION_ID", "OPENCODE_SESSION_ID"):
            os.environ.pop(key, None)
        runner = mock.patch.object(peer_steward.peer_obligations, "ensure_normal_runner", return_value=False)
        runner.start()
        self.addCleanup(runner.stop)
        # These tests replace herdr's RPC and native observations below. Declare
        # the executable boundary too; the isolated runner strips workstation PATH.
        herdr = mock.patch.object(peer_steward.shutil, "which", return_value="/usr/bin/herdr")
        herdr.start()
        self.addCleanup(herdr.stop)
        self._readiness_patch = mock.patch.object(
            peer_steward, "_pane_readiness",
            return_value=SimpleNamespace(state="ready", reason=""))
        self._readiness_patch.start()
        self.addCleanup(self._readiness_patch.stop)
        self._native_sid_by_harness = {}
        self._native_sid_by_pid = {}
        self._codex_server_observations = []
        self._pane_pid = {}
        self._census_panes = [{"pane_id": "w1:pX", "agent": "claude",
                               "agent_session": {"kind": "id", "value": "sid-child"},
                               "agent_status": "idle", "cwd": "/fixture"}]

        def census(session=None):
            return SimpleNamespace(
                endpoint=peer_steward.herdr_observation.endpoint_name(session),
                complete=True, observed_at=time.monotonic(),
                panes=tuple(dict(row) for row in self._census_panes))

        self._census_patch = mock.patch.object(
            peer_steward.herdr_observation, "pane_census", side_effect=census)
        self._census_patch.start()
        self.addCleanup(self._census_patch.stop)

        from fleet import process_identity as native_identity

        def process_identity(pid, harness):
            sid = self._native_sid_by_pid.get(pid, self._native_sid_by_harness.get(harness))
            if sid:
                return SimpleNamespace(confidence=native_identity.PROVEN, session_id=sid)
            return SimpleNamespace(confidence=native_identity.HARNESS_ONLY, session_id="")

        self._native_identity_patch = mock.patch.object(
            native_identity, "process_identity", side_effect=process_identity)
        self._native_identity_patch.start()
        self.addCleanup(self._native_identity_patch.stop)
        # Patch the module function where the production helper imports it.
        import pane_ownership
        self._codex_identity_patch = mock.patch.object(
            pane_ownership, "current_codex_thread",
            side_effect=lambda _pane, pid=None, server=None, **_kw:
            (self._codex_server_observations.append(server),
             self._native_sid_by_pid.get(pid, self._native_sid_by_harness.get("codex", "")) or "")[1])
        self._codex_identity_patch.start()
        self.addCleanup(self._codex_identity_patch.stop)

    def _set_inventory(self, rows):
        self._listed_rows = list(rows)
        self._census_panes = []
        for index, row in enumerate(rows):
            pid = 40000 + index
            self._pane_pid[row["pane_id"]] = pid
            sid = row.get("agent_session") or {}
            sid = sid.get("value") if isinstance(sid, dict) else None
            if sid:
                self._native_sid_by_pid[pid] = sid
                self._native_sid_by_harness[row["agent"]] = sid
            census = {"pane_id": row["pane_id"], "agent": row["agent"],
                      "agent_status": "idle", "cwd": "/fixture"}
            if sid:
                census["agent_session"] = {"kind": "id", "value": sid}
            self._census_panes.append(census)

    def _target_for(self, pane):
        row = next((item for item in self._census_panes if item["pane_id"] == pane), None)
        if row is None:
            return "agent-not-found", {"harness": "-", "session_id": "-",
                                       "name": pane, "pane": "-"}, 2, None
        listed = next((item for item in getattr(self, "_listed_rows", ())
                       if item.get("pane_id") == pane), {})
        identity = row.get("agent_session") or {}
        return row.get("agent_status", "idle"), {
            "harness": row["agent"], "session_id": identity.get("value") or "-",
            "name": listed.get("name") or pane, "pane": pane}, 0, None

    def _pane_info_for(self, pane):
        pid = self._pane_pid.setdefault(pane, 41000 + len(self._pane_pid))
        return {"pane_id": pane, "shell_pid": os.getpid(),
                "foreground_process_group_id": pid,
                "foreground_processes": [{"pid": pid, "argv": ["claude"]}]}

    def _fake_run(self, payload, calls=None):
        agent = payload["result"]["agent"]
        self._listed_rows = [agent]
        self._set_inventory([agent])
        sid = (agent.get("agent_session") or {}).get("value")
        if sid:
            self._native_sid_by_pid[os.getpid()] = sid
            # The pane's own foreground pid carries the same native session; the
            # OpenCode send path now proves it exactly like Claude's.
            self._native_sid_by_pid.setdefault(
                self._pane_pid.get(agent.get("pane_id"), 40000), sid)
            self._native_sid_by_harness[agent.get("agent")] = sid

        def run(argv, **kwargs):
            if calls is not None:
                calls.append(argv)
            command = list(argv)
            if command[:2] == ["herdr", "--session"]:
                command = command[3:]
            elif command[:1] == ["herdr"]:
                command = command[1:]
            if command[:2] == ["agent", "list"]:
                return _herdr_json({"result": {"agents": [agent]}})
            if command[:2] == ["pane", "list"]:
                return _herdr_json({"result": {"panes": self._census_panes}})
            if command[:2] == ["agent", "get"]:
                return _herdr_json(payload)
            if command[:2] == ["pane", "process-info"]:
                pid = os.getpid()
                return _herdr_json({"result": {"process_info": {
                    "pane_id": "w1:pX", "shell_pid": pid,
                    "foreground_process_group_id": os.getpgrp(),
                    "foreground_processes": [{"pid": pid, "argv": ["python3", "peer_steward.test"]}],
                }}})
            return subprocess.CompletedProcess(argv, 0, stdout="{}", stderr="")
        return run

    @staticmethod
    def _codex_process_info(pids, pane="w1:pX"):
        return {"pane_id": pane, "shell_pid": os.getpid(),
                "foreground_process_group_id": pids[0],
                "foreground_processes": [{"pid": pid, "argv": ["codex"]} for pid in pids]}

    def _run_immediate_codex_restart(self, scenario):
        self._readiness_patch.stop()
        peer_steward._HERDR_SESSION = "selected"
        sid = "codex-thread-original"
        replacement_sid = "codex-thread-replacement"
        payload = _agent_json("codex", sid, "child", status="idle")
        calls, proofs, submissions = [], [], []
        starts = {41001: "birth-before-restart", 41002: "birth-after-restart",
                  41003: "birth-unproven"}
        info_rows = [self._codex_process_info([41001]),
                     self._codex_process_info([41002]),
                     self._codex_process_info([41002])]
        info_rows.append(self._codex_process_info(
            [41002, 41003] if scenario == "conflicting-proof"
            else [41003] if scenario == "unknown-proof" else [41002]))
        info_index = 0
        target_calls = 0
        native_calls = 0

        def pane_info(_pane):
            nonlocal info_index
            info = info_rows[min(info_index, len(info_rows) - 1)]
            info_index += 1
            return info

        def target(pane):
            nonlocal target_calls
            target_calls += 1
            current_pane = ("w1:pReplacement" if scenario == "changed-pane"
                            and target_calls >= 3 else pane)
            return "idle", {"harness": "codex", "session_id": sid,
                             "name": "child", "pane": current_pane}, 0, None

        def native_thread(_pane, pid=None, **_kwargs):
            nonlocal native_calls
            native_calls += 1
            proofs.append((pid, starts.get(pid)))
            if scenario == "changed-sid" and native_calls >= 3:
                return replacement_sid
            if scenario == "unknown-proof" and native_calls >= 3:
                return ""
            if scenario == "conflicting-proof" and pid == 41003:
                return replacement_sid
            return sid

        def foreground(_pane, harness, *, strict=True, info=None):
            pid = info["foreground_processes"][0]["pid"]
            return {"pid": pid, "start": starts[pid], "argv": [harness]}

        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(peer_steward.subprocess, "run",
                                                   side_effect=self._fake_run(payload, calls)))
            stack.enter_context(mock.patch.object(peer_steward, "_retire_target", side_effect=target))
            stack.enter_context(mock.patch.object(peer_steward, "_retire_pane_info", side_effect=pane_info))
            stack.enter_context(mock.patch.object(peer_steward, "_prompt_foreground", side_effect=foreground))
            stack.enter_context(mock.patch.object(peer_steward, "_current_session_identity",
                                                   return_value=("sender-session", "claude")))
            stack.enter_context(mock.patch.object(peer_steward, "_from_name", return_value="sender"))
            stack.enter_context(mock.patch.object(peer_steward.peer_obligations,
                                                   "bound_work_for_pane", return_value=((), "observed")))
            import pane_ownership
            stack.enter_context(mock.patch.object(pane_ownership, "current_codex_thread",
                                                   side_effect=native_thread))
            stack.enter_context(mock.patch("codex_queue_dispatch.resolve_queue_socket",
                                           return_value="fixture-socket"))
            stack.enter_context(mock.patch(
                "codex_queue_delivery.send_at_least_once",
                side_effect=lambda _socket, **kwargs:
                (submissions.append(kwargs) or {"status": "queued"})))
            stack.enter_context(mock.patch("builtins.print"))
            rc = peer_steward.main([
                "prompt", "child", "[steer] same Codex thread through a process restart"])

        record = self._all_records()[-1]
        ref = record["transfer_ref"]
        return rc, proofs, submissions, ref, peer_steward.peer_message._read_pending(ref)

    def _run_deferred_codex_restart(self, scenario):
        self._readiness_patch.stop()
        peer_steward._HERDR_SESSION = "selected"
        sid = "codex-thread-original"
        replacement_sid = "codex-thread-replacement"
        pane = "w1:pDeferred"
        sender = {"harness": "claude", "session_id": "sender-session", "name": "sender"}
        recipient = {"harness": "codex", "session_id": sid,
                     "name": "child", "pane_id": pane}
        _text, ref = peer_steward.peer_message.prepare_peer_message(
            "[steer] same Codex thread through deferred resume", sender, recipient, defer=True)
        row = peer_steward.peer_message._read_pending(ref)
        store = peer_steward.peer_obligations.ObligationStore()
        duty = store.create("message-" + ref, "message",
            {"server": "selected", "pane": pane, "harness": "codex", "session_id": sid},
            {"ref": ref, "body_digest": row["body_sha256"], "target": pane,
             "from": sender, "to": row["to"]})
        starts = {42001: "birth-before-restart", 42002: "birth-after-restart",
                  42003: "birth-unproven"}
        info_rows = [self._codex_process_info([42001], pane),
                     self._codex_process_info([42002], pane)]
        info_rows.append(self._codex_process_info(
            [42002, 42003] if scenario == "conflicting-proof"
            else [42003] if scenario == "unknown-proof" else [42002], pane))
        info_index = 0
        target_calls = 0
        native_calls = 0
        proofs, submissions = [], []

        def pane_info(_pane):
            nonlocal info_index
            info = info_rows[min(info_index, len(info_rows) - 1)]
            info_index += 1
            return info

        def target(_target):
            nonlocal target_calls
            target_calls += 1
            current_pane = ("w1:pReplacement" if scenario == "changed-pane"
                            and target_calls >= 2 else pane)
            current_sid = (replacement_sid if scenario == "changed-sid"
                           and target_calls >= 2 else sid)
            return "idle", {"harness": "codex", "session_id": current_sid,
                             "name": "child", "pane": current_pane}, 0, None

        def native_thread(_pane, pid=None, **_kwargs):
            nonlocal native_calls
            native_calls += 1
            proofs.append((pid, starts.get(pid)))
            if scenario == "changed-sid" and native_calls >= 2:
                return replacement_sid
            if scenario == "unknown-proof" and native_calls >= 2:
                return ""
            if scenario == "conflicting-proof" and pid == 42003:
                return replacement_sid
            return sid

        def foreground(_pane, harness, *, strict=True, info=None):
            pid = info["foreground_processes"][0]["pid"]
            return {"pid": pid, "start": starts[pid], "argv": [harness]}

        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(peer_steward, "_retire_target", side_effect=target))
            stack.enter_context(mock.patch.object(peer_steward, "_retire_pane_info", side_effect=pane_info))
            stack.enter_context(mock.patch.object(peer_steward, "_prompt_foreground", side_effect=foreground))
            stack.enter_context(mock.patch.object(peer_steward.peer_obligations,
                                                   "bound_work_for_pane", return_value=((), "observed")))
            import pane_ownership
            stack.enter_context(mock.patch.object(pane_ownership, "current_codex_thread",
                                                   side_effect=native_thread))
            stack.enter_context(mock.patch("codex_queue_dispatch.resolve_queue_socket",
                                           return_value="fixture-socket"))
            stack.enter_context(mock.patch(
                "codex_queue_delivery.send_at_least_once",
                side_effect=lambda _socket, **kwargs:
                (submissions.append(kwargs) or {"status": "queued"})))
            peer_steward._resume_message_obligation(duty, store)

        return store, duty, proofs, submissions, ref, peer_steward.peer_message._read_pending(ref)

    @staticmethod
    def _pane_io_calls(calls):
        return [argv for argv in calls if argv[:3] in (
            ["herdr", "agent", "prompt"], ["herdr", "pane", "send-text"],
            ["herdr", "pane", "send-keys"], ["herdr", "agent", "send-text"],
            ["herdr", "agent", "send-keys"],
        )]

    @staticmethod
    def _inventory_row(harness="claude", sid="sid-child", name="child", pane="w1:pX"):
        return {"agent": harness, "name": name, "pane_id": pane,
                "agent_session": ({"agent": harness, "kind": "id", "value": sid}
                                  if sid else None)}

    @staticmethod
    def _current_target(harness="claude", sid="sid-child", name="child", pane="w1:pX"):
        return "idle", {"harness": harness, "session_id": sid or "-",
                         "name": name, "pane": pane}, 0, None

    @contextlib.contextmanager
    def _current_claude_resume(self, *, fork=False):
        """Real shared registry/tap reader; private native files and observed own PID."""
        from fleet.collectors import claude, procscan
        self._native_identity_patch.stop()
        self._readiness_patch.stop()
        pane, pid = "w1:pResume", os.getpid()
        old = "11111111-1111-4111-8111-111111111111"
        new = "22222222-2222-4222-8222-222222222222"
        start = peer_steward._proc_start_ticks(pid)
        home = self.tmp_root / "native-claude"
        (home / "sessions").mkdir(exist_ok=True, parents=True)
        (home / ".statusline").mkdir(exist_ok=True)
        registry = home / "sessions" / f"{pid}.json"
        argv = ["/usr/bin/claude", "--resume", old] + (["--fork-session"] if fork else [])
        foreground = {"pid": pid, "start": start, "argv": argv}
        info = {"pane_id": pane, "shell_pid": pid,
                "foreground_processes": [{"pid": pid, "argv": argv}]}
        observation = {"state": "idle", "label": old, "screen": CLAUDE_EMPTY}

        def select(mode):
            registry.unlink(missing_ok=True)
            for path in (home / ".statusline").glob("*.json"):
                path.unlink()
            if mode.startswith("registry"):
                sid = new if mode == "registry-new" else old
                registry.write_text(json.dumps({"pid": pid, "procStart": start,
                    "sessionId": sid, "cwd": str(home)}))
            taps = ([old, new] if mode == "conflict" else
                    [new] if mode == "tap-new" else [old] if mode == "tap" else [])
            for sid in taps:
                (home / ".statusline" / f"{sid}.json").write_text(json.dumps(
                    {"pid": pid, "proc_start": start, "session_id": sid}))

        def target(_target):
            return observation["state"], {"harness": "claude", "session_id": observation["label"],
                "pane": pane, "name": "resumed"}, 0, None

        select("registry")
        with mock.patch.object(claude, "_home", return_value=str(home)), \
             mock.patch.object(procscan, "_comm_of", return_value="fixture"), \
             mock.patch.object(procscan, "_read_cwd", return_value=(str(home), None)), \
             mock.patch.object(procscan.claude_process_identity, "observe_process", return_value=None), \
             mock.patch.object(peer_steward, "_retire_target", side_effect=target), \
             mock.patch.object(peer_steward, "_retire_pane_info", return_value=info), \
             patch_split(peer_steward, "_retire_process_record", return_value=foreground), \
             mock.patch.object(peer_steward, "_prompt_foreground", return_value=foreground), \
             mock.patch.object(peer_steward, "_agent_state",
                               side_effect=lambda _: (observation["state"], pane)), \
             mock.patch.object(peer_steward, "_read_screen",
                 side_effect=lambda _: peer_steward._screen_lines(observation["screen"])), \
             mock.patch.object(peer_steward.peer_obligations, "bound_work_for_pane",
                               return_value=((), "observed")), \
             mock.patch.object(peer_steward, "_herdr_prompt", return_value=(0, {})) as send:
            yield pane, old, new, select, observation, send

    def test_claude_resume_uses_current_pid_start_registry_and_tap_evidence(self):
        with self._current_claude_resume() as (pane, old, new, select, observation, send):
            for mode in ("registry", "tap"):
                with self.subTest(mode=mode):
                    select(mode)
                    candidate, error = peer_steward._prompt_current_candidate(
                        pane, expected_harness="claude", expected_sid=old)
                    self.assertIsNone(error)
                    self.assertEqual(candidate["session_id"], old)
                    reason, proof = peer_steward._native_idle_prompt_guard(pane, "claude", old, pane)
                    self.assertIsNone(reason)
                    self.assertEqual(proof["session_id"], old)
            # A PID-bound current selection outranks the delayed launch/pane label.
            select("registry-new")
            candidate, error = peer_steward._prompt_current_candidate(pane, expected_sid=new)
            self.assertIsNone(error)
            self.assertEqual(candidate["session_id"], new)
            send.assert_not_called()

    def test_claude_resume_stale_pin_has_zero_writes_at_admission_replay_and_final_guard(self):
        peer = peer_steward.peer_message
        for phase in ("admission", "replay", "final-prewrite"):
            for mode in ("registry-new", "tap-new", "conflict", "unavailable"):
                for fork in (False, True):
                    with self.subTest(phase=phase, mode=mode, fork=fork), \
                         self._current_claude_resume(fork=fork) as fixture:
                        pane, old, new, select, observation, send = fixture
                        sender = {"harness": "codex", "session_id": "requester"}
                        recipient = {"harness": "claude", "session_id": old, "pane_id": pane}
                        text, ref = peer.prepare_peer_message(
                            f"[steer] {phase}/{mode}/{fork}", sender, recipient, defer=True)
                        row = peer._read_pending(ref)
                        duty = peer_steward._schedule_message_obligation(row, pane, pane)
                        store = peer_steward.peer_obligations.ObligationStore()
                        immutable = {key: row.get(key) for key in
                            ("ref", "from", "to", "text", "body_sha256", "producer_binding")}
                        reserve = peer.reserve_pending_herdr
                        def reserve_then_select(*args, **kwargs):
                            claimed = reserve(*args, **kwargs)
                            self.assertIsNotNone(claimed)
                            select(mode)
                            return claimed
                        if phase != "final-prewrite":
                            select(mode)
                        with mock.patch.object(peer, "reserve_pending_herdr",
                                side_effect=reserve_then_select if phase == "final-prewrite" else reserve) as claim, \
                             mock.patch.object(peer, "begin_pending_write", wraps=peer.begin_pending_write) as begin:
                            if phase == "admission":
                                candidate, error = peer_steward._prompt_current_candidate(
                                    pane, expected_harness="claude", expected_sid=old)
                                self.assertIsNone(candidate)
                                self.assertIn(error, {"target-identity-unverified", "target-identity-changed"})
                                peer_steward._submit_pending_native(row, pane, old, pane, "idle", harness="claude")
                            else:
                                peer_steward._resume_message_obligation(duty, store)
                            self.assertEqual(claim.call_count, int(phase == "final-prewrite"))
                            begin.assert_not_called()
                        send.assert_not_called()
                        after = peer._read_pending(ref)
                        self.assertEqual(after["state"], "pending")
                        self.assertEqual({key: after.get(key) for key in immutable}, immutable)
                        self.assertEqual(store.get(duty["id"])["intent"]["ref"], ref)
                        self.assertEqual(len(store.list(kinds={"message"})), 1)

                        # The same controller reobserves current evidence; one submission
                        # leaves the original marker/ref queued and then receive-only.
                        select("registry")
                        peer_steward._resume_message_obligation(store.get(duty["id"]), store)
                        peer_steward._resume_message_obligation(store.get(duty["id"]), store)
                        self.assertEqual(send.call_count, 1)
                        self.assertEqual(send.call_args.args[:2], (pane, text))
                        self.assertEqual(peer._read_pending(ref)["state"], "queued")
                        self.assertEqual({key: peer._read_pending(ref).get(key) for key in immutable}, immutable)
                        # Retire only this case's terminal fixture duty, keeping other
                        # cases out of the controller count without deleting any rows.
                        store.update(duty["id"], state="complete", result="fixture-finished")

    def test_current_claude_resume_keeps_form_draft_and_busy_guards_before_physical_input(self):
        peer = peer_steward.peer_message
        for phase in ("admission", "final-prewrite"):
            for case, state, screen in (("form", "idle", CLAUDE_FORM),
                                        ("draft", "idle", CLAUDE_DRAFT),
                                        ("busy", "working", CLAUDE_EMPTY)):
                with self.subTest(phase=phase, case=case), self._current_claude_resume() as fixture:
                    pane, old, _new, _select, observation, send = fixture
                    _text, ref = peer.prepare_peer_message(f"[steer] protect {phase}/{case}",
                        {"harness": "codex", "session_id": "requester"},
                        {"harness": "claude", "session_id": old, "pane_id": pane}, defer=True)
                    before = peer._read_pending(ref)
                    reserve = peer.reserve_pending_herdr
                    def reserve_then_block(*args, **kwargs):
                        claimed = reserve(*args, **kwargs)
                        observation.update(state=state, screen=screen)
                        return claimed
                    if phase == "admission":
                        observation.update(state=state, screen=screen)
                    with mock.patch.object(peer, "reserve_pending_herdr",
                            side_effect=reserve_then_block if phase == "final-prewrite" else reserve) as claim, \
                         mock.patch.object(peer, "begin_pending_write", wraps=peer.begin_pending_write) as begin:
                        reason, _outcome, _verify, _why = peer_steward._submit_pending_native(
                            before, pane, old, pane, "idle", harness="claude")
                        self.assertIsNotNone(reason)
                        self.assertEqual(claim.call_count, int(phase == "final-prewrite"))
                        begin.assert_not_called()
                    send.assert_not_called()
                    after = peer._read_pending(ref)
                    self.assertEqual(after["state"], "pending")
                    for key in ("ref", "from", "to", "text", "body_sha256"):
                        self.assertEqual(after[key], before[key])

    def test_unsent_claude_message_reenters_guarded_sender_once_then_only_observes(self):
        self._readiness_patch.stop()
        peer = peer_steward.peer_message
        pane, sid, pid = "w1:pReplay", "resumed-native", os.getpid()
        sender = {"harness": "codex", "session_id": "requester"}
        recipient = {"harness": "claude", "session_id": sid, "pane_id": pane}
        self._native_sid_by_pid[pid] = sid
        text, ref = peer.prepare_peer_message("[steer] correction", sender, recipient, defer=True)
        row = peer._read_pending(ref)
        store = peer_steward.peer_obligations.ObligationStore()
        duty = peer_steward._schedule_message_obligation(row, pane, pane)
        info = {"pane_id": pane, "shell_pid": pid, "foreground_process_group_id": pid,
                "foreground_processes": [{"pid": pid, "argv": ["claude", "--resume", sid]}]}
        foreground = {"pid": pid, "start": peer_steward._proc_start_ticks(pid), "argv": ["claude"]}
        with mock.patch.object(peer_steward, "_retire_target", return_value=("idle", {
                "harness": "claude", "session_id": "stale-label", "pane": pane, "name": "new"}, 0, None)), \
             mock.patch.object(peer_steward, "_retire_pane_info", return_value=info), \
             mock.patch.object(peer_steward, "_prompt_foreground", return_value=foreground), \
             mock.patch.object(peer_steward, "_agent_state", return_value=("idle", pane)), \
             mock.patch.object(peer_steward, "_read_screen", return_value=peer_steward._screen_lines(CLAUDE_EMPTY)), \
             mock.patch.object(peer_steward, "_herdr_prompt", return_value=(0, {})) as send:
            peer_steward._resume_message_obligation(duty, store)
            peer_steward._resume_message_obligation(store.get(duty["id"]), store)
        self.assertEqual(send.call_count, 1)
        self.assertEqual(send.call_args.args[:2], (pane, text))
        self.assertEqual(peer._read_pending(ref)["state"], "queued")

    def test_name_resolution_uses_matching_native_proof_when_inventory_label_is_late(self):
        row = self._inventory_row(sid="late-label", name="child")
        self._set_inventory([row])
        self._census_panes[0]["agent_session"]["value"] = "current-native"
        self._native_sid_by_pid[40000] = "current-native"
        stale = ("idle", {"harness": "claude", "session_id": "late-label",
                          "name": "child", "pane": "w1:pX"}, 0, None)
        with mock.patch.object(peer_steward, "_prompt_agent_inventory", return_value=([row], None)), \
             mock.patch.object(peer_steward, "_retire_target", return_value=stale), \
             mock.patch.object(peer_steward, "_retire_pane_info", side_effect=self._pane_info_for):
            current, error, _panes = peer_steward._resolve_prompt_target("child")
            self.assertIsNone(error)
            self.assertEqual(current["session_id"], "current-native")
            self._native_sid_by_pid[40000] = "actually-other"
            current, error, _panes = peer_steward._resolve_prompt_target("child")
            self.assertIsNone(current)
            self.assertEqual(error, "target-identity-changed")

    def test_native_process_wins_over_stale_label_but_other_native_sid_is_protected(self):
        ident = {"harness": "claude", "session_id": "stale-label", "pane": "w1:pX", "name": "peer"}
        info = self._pane_info_for("w1:pX")
        pid = info["foreground_processes"][0]["pid"]
        self._native_sid_by_pid[pid] = "expected"
        candidate, error = peer_steward._prompt_current_candidate(
            "w1:pX", expected_harness="claude", expected_sid="expected",
            target_observation=("idle", ident), pane_info=info)
        self.assertIsNone(error)
        self.assertEqual(candidate["session_id"], "expected")
        self._native_sid_by_pid[pid] = "other-native"
        candidate, error = peer_steward._prompt_current_candidate(
            "w1:pX", expected_sid="expected", target_observation=("idle", ident), pane_info=info)
        self.assertIsNone(candidate)
        self.assertEqual(error, "target-identity-changed")

    def _native_address_retry_observations(self, stack):
        self._readiness_patch.stop()
        peer_steward._HERDR_SESSION = "selected"
        row = self._inventory_row(harness="codex", sid="exact-native-thread")
        self._set_inventory([row])
        stack.enter_context(mock.patch.object(peer_steward, "_prompt_agent_inventory", side_effect=lambda: (self._listed_rows, None)))
        stack.enter_context(mock.patch.object(peer_steward, "_retire_target", side_effect=self._target_for))
        stack.enter_context(mock.patch.object(peer_steward, "_retire_pane_info", side_effect=self._pane_info_for))
        process = stack.enter_context(mock.patch.object(peer_steward, "_prompt_process_session", return_value=(None, None)))
        stack.enter_context(mock.patch.object(peer_steward, "_prompt_foreground", return_value=None))
        stack.enter_context(mock.patch.object(peer_steward.peer_obligations, "bound_work_for_pane", return_value=((), "observed")))
        stack.enter_context(mock.patch.object(peer_steward, "_current_session_identity", return_value=("original-sender", "claude")))
        stack.enter_context(mock.patch.object(peer_steward, "_from_name", return_value="sender"))
        stack.enter_context(mock.patch("codex_queue_dispatch.resolve_queue_socket", return_value="fixture-socket"))
        stack.enter_context(mock.patch("builtins.print"))
        return process

    def test_native_queue_failure_duty_recovers_without_process_sid_and_preserves_wire(self):
        import codex_queue_delivery as queue
        peer = peer_steward.peer_message
        for failure in ("retryable", "unverified", "raises", "rpc-error"):
            with self.subTest(failure=failure), contextlib.ExitStack() as stack:
                process = self._native_address_retry_observations(stack)
                # Older same-sender rows from sibling subtests would be delivered first;
                # this fixture counts one row's RPC sequence (promotion has its own tests).
                stack.enter_context(mock.patch.object(peer, "_promote_prior_codex_rows", return_value=[]))
                submissions = []
                def submit(_socket, **kwargs):
                    submissions.append(kwargs)
                    kwargs["before_add"]()
                    return {"status": "queued"}
                send = stack.enter_context(mock.patch.object(queue, "send_at_least_once", side_effect=submit))
                args = peer_steward.build_parser().parse_args([
                    "prompt", "child", "original correction " + failure, "--ref", "original-ref"])
                if failure == "rpc-error":
                    with mock.patch.object(queue, "send_at_least_once", side_effect=queue.QueueDeliveryError("queue-outage")):
                        code = peer_steward.cmd_prompt(args)
                else:
                    with mock.patch.object(peer, "deliver_pending_codex",
                            side_effect=OSError("queue-outage") if failure == "raises" else None,
                            return_value={"status": failure, "reason": "queue-outage"}):
                        code = peer_steward.cmd_prompt(args)
                self.assertEqual(code, 5)
                store = peer_steward.peer_obligations.ObligationStore()
                duties = [d for d in store.list() if d["intent"].get("ref") == self._all_records()[-1]["transfer_ref"]]
                self.assertEqual(len(duties), 1)
                duty, = duties
                ref = duty["intent"]["ref"]
                original = peer._read_pending(ref)
                self.assertEqual(original["state"], "pending")
                self.assertEqual(duty["intent"]["identity"]["pane"], "w1:pX")
                self.assertEqual(duty["intent"]["identity"]["server"], "selected")
                peer_steward._resume_message_obligation(duty, store)
                self.assertEqual(send.call_count, 1)
                self.assertEqual(submissions[0]["thread_id"], "exact-native-thread")
                self.assertEqual(submissions[0]["client_message_id"], "delivery-peer-" + ref)
                self.assertEqual(submissions[0]["message"], original["text"])
                self.assertEqual(process.return_value, (None, None))
                latest = peer._read_pending(ref)
                for key in ("ref", "text", "refs", "body_sha256", "source_sha256", "from", "to"):
                    self.assertEqual(latest[key], original[key])
                self.assertEqual(latest["state"], "queued")
                peer_steward._resume_message_obligation(store.get(duty["id"]), store)
                self.assertEqual(send.call_count, 1)  # Submitted rows are observed only.
                self.assertEqual(store.get(duty["id"])["state"], "unknown")

    def test_unresolved_native_address_becomes_exact_in_the_existing_duty(self):
        import codex_queue_delivery as queue
        peer = peer_steward.peer_message
        with contextlib.ExitStack() as stack:
            self._native_address_retry_observations(stack)
            saved_rows = self._listed_rows
            self._set_inventory([])
            args = peer_steward.build_parser().parse_args(["prompt", "child", "preserved unresolved correction"])
            send = stack.enter_context(mock.patch.object(queue, "send_at_least_once", return_value={"status": "queued"}))
            self.assertEqual(peer_steward.cmd_prompt(args), 5)
            store = peer_steward.peer_obligations.ObligationStore()
            duty, = [d for d in store.list() if d["intent"]["kind"] == "message"]
            original = peer._read_pending(duty["intent"]["ref"])
            send.assert_not_called()
            self._set_inventory(saved_rows)
            peer_steward._resume_message_obligation(duty, store)
            self.assertEqual(send.call_count, 1)
            row = peer._read_pending(duty["intent"]["ref"])
            self.assertEqual(row["to"], original["to"])
            self.assertEqual(row["text"], original["text"])
            self.assertEqual(row["body_sha256"], original["body_sha256"])
            self.assertEqual(peer.pending_delivery_recipient(row)["session_id"], "exact-native-thread")

    def test_unresolved_retries_keep_one_ref_duty_and_native_submission_on_caller_or_runner_recovery(self):
        import codex_queue_delivery as queue
        peer = peer_steward.peer_message
        for recovery, submitted in ((r, s) for r in ("caller", "runner") for s in ("queued", "ambiguous")):
            with self.subTest(recovery=recovery, submitted=submitted), contextlib.ExitStack() as stack:
                self._native_address_retry_observations(stack)
                # Older same-sender rows from sibling subtests would be delivered first;
                # this fixture counts one row's RPC sequence (promotion has its own tests).
                stack.enter_context(mock.patch.object(peer, "_promote_prior_codex_rows", return_value=[]))
                saved_rows = self._listed_rows
                self._set_inventory([])
                args = peer_steward.build_parser().parse_args([
                    "prompt", "child", "original unresolved " + recovery + submitted, "--ref", "stable-request"])
                store = peer_steward.peer_obligations.ObligationStore()
                submissions = []
                observations = []
                def submit(_socket, **kwargs):
                    observations.append(kwargs["allow_add"])
                    if kwargs["allow_add"]:
                        kwargs["before_add"]()
                        submissions.append(kwargs)
                        if submitted == "ambiguous":
                            raise queue.QueueDeliveryError("submitted-but-unverified", ambiguous=True)
                    return {"status": "queued"}
                stack.enter_context(mock.patch.object(queue, "send_at_least_once", side_effect=submit))
                for _ in range(3):
                    self.assertEqual(peer_steward.cmd_prompt(args), 5)
                duties = [d for d in store.list()
                          if d["intent"].get("refs") == ["stable-request"]
                          and peer._read_pending(d["intent"]["ref"])["source_sha256"]
                          == hashlib.sha256(args.text.encode()).hexdigest()]
                duty, = duties
                original = peer._read_pending(duty["intent"]["ref"])
                self.assertFalse(submissions)
                self._set_inventory(saved_rows)
                if recovery == "caller":
                    self.assertEqual(peer_steward.cmd_prompt(args), 3 if submitted == "queued" else 5)
                else:
                    peer_steward._resume_message_obligation(duty, store)
                row = peer._read_pending(original["ref"])
                self.assertEqual(len(submissions), 1)
                self.assertEqual(submissions[0]["thread_id"], "exact-native-thread")
                self.assertEqual(submissions[0]["client_message_id"], "delivery-peer-" + original["ref"])
                for key in ("ref", "text", "from", "to", "refs", "body_sha256", "source_sha256"):
                    self.assertEqual(row[key], original[key])
                # A later outage and both caller/runner retries remain receipt-only.
                self._set_inventory([])
                self.assertEqual(peer_steward.cmd_prompt(args), 5)
                self._set_inventory(saved_rows)
                peer_steward.cmd_prompt(args)
                peer_steward._resume_message_obligation(store.get(duty["id"]), store)
                self.assertEqual(len(submissions), 1)
                self.assertEqual(len([d for d in store.list() if d["intent"].get("ref") == original["ref"]]), 1)
                self.assertEqual(len([r for r in peer._pending_rows()
                                      if r["source_sha256"] == original["source_sha256"]]), 1)
                self.assertEqual(observations, [True, False])
                sys.stdout.write("peer-retry-probe=" + json.dumps({"recovery": recovery,
                    "submitted": submitted, "ref": original["ref"], "duties": 1,
                    "native_adds": len(submissions), "allow_add": observations,
                    "thread_id": submissions[0]["thread_id"], "server": original["server"],
                    "body_sha256": original["body_sha256"], "original_wire_preserved": True}) + "\n")

    def test_exact_request_survives_outages_namesakes_and_caller_or_runner_recovery(self):
        import codex_queue_delivery as queue
        peer = peer_steward.peer_message
        for initial in ("pending", "queued", "ambiguous"):
            for recovery in ("caller", "runner"):
                for address in ("child", "w1:pX", "[3e]"):
                    with self.subTest(initial=initial, recovery=recovery, address=address), contextlib.ExitStack() as stack:
                        self._native_address_retry_observations(stack)
                        stack.enter_context(mock.patch.object(peer_steward, "_prompt_fleet_tag", return_value=("3e", None)))
                        inventory = self._listed_rows
                        args = peer_steward.build_parser().parse_args([
                            "prompt", address, "exact outage " + initial + recovery + address,
                            "--ref", "stable-exact-request"])
                        adds, observations = [], []
                        def submit(_socket, **kw):
                            observations.append(kw["allow_add"])
                            if kw["allow_add"]:
                                kw["before_add"]()
                                adds.append(kw)
                                if initial == "ambiguous":
                                    raise queue.QueueDeliveryError("submitted-but-unverified", ambiguous=True)
                            return {"status": "queued"}
                        stack.enter_context(mock.patch.object(queue, "send_at_least_once", side_effect=submit))
                        binder = stack.enter_context(mock.patch.object(peer, "bind_unresolved_pending_recipient",
                                                                      wraps=peer.bind_unresolved_pending_recipient))
                        if initial == "pending":
                            with mock.patch.object(peer, "deliver_pending_codex", side_effect=OSError("queue-outage")):
                                codes = [peer_steward.cmd_prompt(args)]
                        else:
                            codes = [peer_steward.cmd_prompt(args)]
                        store = peer_steward.peer_obligations.ObligationStore()
                        duty, = [d for d in store.list() if d["intent"].get("refs") == args.ref
                                 and peer._read_pending(d["intent"]["ref"])["source_sha256"]
                                 == hashlib.sha256(args.text.encode()).hexdigest()]
                        original = peer._read_pending(duty["intent"]["ref"])
                        transfer = peer._transfer_path(original["ref"]).read_bytes()
                        def retained():
                            rows = [r for r in peer._pending_rows() if r["source_sha256"] == original["source_sha256"]]
                            self.assertEqual(len(rows), 1)
                            for key in ("ref", "text", "refs", "from", "to", "body_sha256", "source_sha256"):
                                self.assertEqual(rows[0][key], original[key])
                            self.assertEqual(peer._transfer_path(original["ref"]).read_bytes(), transfer)
                            self.assertEqual(store.get(duty["id"])["intent"], duty["intent"])
                            return rows[0]
                        for _ in range(3):
                            self._set_inventory([])
                            codes.append(peer_steward.cmd_prompt(args))
                            self.assertEqual(retained(), original)  # Submission evidence is unchanged.
                        # A current namesake, even if exactly resolved, cannot replace the pin.
                        for rows in ([self._inventory_row(harness="codex", sid="other-native")],
                                     [self._inventory_row(harness="codex", sid="other-native"),
                                      self._inventory_row(harness="codex", sid="another-native", pane="w1:pY")]):
                            # The pin still exists elsewhere: these observations
                            # prove a different occupant, not terminal pin loss.
                            self._set_inventory(rows + [self._inventory_row(harness="codex",
                                sid="exact-native-thread", name="original moved", pane="w1:pOriginal")])
                            codes.append(peer_steward.cmd_prompt(args))
                            self.assertEqual(codes[-1], 5)
                            peer_steward._resume_message_obligation(store.get(duty["id"]), store)
                            self.assertEqual(len(adds), 0 if initial == "pending" else 1)
                            retained()
                        self._set_inventory(inventory)
                        if recovery == "caller":
                            codes.append(peer_steward.cmd_prompt(args))
                        else:
                            peer_steward._resume_message_obligation(store.get(duty["id"]), store)
                        self.assertEqual(len(adds), 1)
                        # Another outage after submission and both retry routes stay receive-only.
                        self._set_inventory([])
                        codes.append(peer_steward.cmd_prompt(args))
                        self._set_inventory(inventory)
                        peer_steward.cmd_prompt(args)
                        peer_steward._resume_message_obligation(store.get(duty["id"]), store)
                        retained()
                        binder.assert_not_called()
                        self.assertEqual(len(adds), 1)
                        self.assertEqual(adds[0]["thread_id"], "exact-native-thread")
                        self.assertEqual(adds[0]["client_message_id"], "delivery-peer-" + original["ref"])
                        self.assertEqual(adds[0]["message"], original["text"])
                        self.assertEqual(len([d for d in store.list() if d["intent"].get("ref") == original["ref"]]), 1)
                        sys.stdout.write("peer-retained-probe=" + json.dumps({
                            "initial": initial, "recovery": recovery, "address": address,
                            "codes": codes, "ref": original["ref"], "rows": 1, "duties": 1,
                            "native_adds": len(adds), "allow_add": observations,
                            "recipient": original["to"], "server": original["server"],
                            "body_sha256": original["body_sha256"], "immutable_preserved": True}) + "\n")

    def test_verified_alias_sequences_keep_the_pin_through_outages_and_reassignment(self):
        for unresolved in (False, True):
            for initial in ("pending", "queued", "ambiguous"):
                for recovery in ("caller", "runner"):
                    with self.subTest(unresolved=unresolved, initial=initial, recovery=recovery):
                        self._verified_alias_sequence(initial, recovery, unresolved=unresolved)

    def _canonical_runtime_notice(self, body, prepared):
        """Use the real notice producer, courier, preparation and owner callback."""
        import session_notice
        obligations = peer_steward.peer_obligations
        with mock.patch.object(obligations, "ensure_normal_runner", return_value=False), \
             mock.patch.object(obligations, "request_dispatch_reconciliation", return_value=False):
            record = session_notice.notify("codex", "exact-native-thread",
                key="alias-courier:" + hashlib.sha256(body.encode()).hexdigest(),
                subject="runtime alias fixture", text=body, jobs=self.jobs_path)
        store = obligations.ObligationStore(self.jobs_path.parent)
        duty, = [d for d in store.list() if d["intent"].get("delivery_id") == record["delivery_id"]]
        command = peer_steward.cmd_prompt
        real_run = peer_steward.subprocess.run
        captured = []
        def transport(argv, *args, **kwargs):
            if "agent" in argv and "list" in argv:
                return SimpleNamespace(returncode=0, stdout=json.dumps({"result": {"agents": self._listed_rows}}))
            if "agent" in argv and "get" in argv:
                match = next((r for r in self._listed_rows if r.get("pane_id") == argv[-1]), {})
                return SimpleNamespace(returncode=0, stdout=json.dumps({"result": {"agent": match}}))
            return real_run(argv, *args, **kwargs)
        def prepare(args, **kwargs):
            original_callback = kwargs["on_prepared"]
            def callback(ref):
                original_callback(ref)
                prepared.append(ref)
            kwargs["on_prepared"] = callback
            text = Path(args.body_file).read_text()
            retained = peer_steward.build_parser().parse_args(["prompt", args.target, text,
                *[item for ref in args.ref for item in ("--ref", ref)]])
            code = command(args, **kwargs)
            captured.append((retained, kwargs, code))
            return code
        with mock.patch.dict(os.environ, {"AGENT_HERDR_SESSION": "selected"}), \
             mock.patch.object(peer_steward.subprocess, "run", side_effect=transport), \
             mock.patch.object(peer_steward, "cmd_prompt", side_effect=prepare):
            peer_steward._reconcile_obligations(store, [duty])
        self.assertEqual(len(captured), 1, store.get(duty["id"]))
        args, kwargs, code = captured[0]
        return args, kwargs, store.get(duty["id"]), code

    def test_typed_short_long_and_runtime_aliases_keep_the_original_callback_and_controller(self):
        for context in ("short", "long", "runtime"):
            for recovery in ("caller", "runner"):
                with self.subTest(context=context, recovery=recovery):
                    self._verified_alias_sequence("queued", recovery, context_kind=context)

    def _verified_alias_sequence(self, initial, recovery, *, unresolved=False, context_kind="ordinary"):
        import codex_queue_delivery as queue
        peer = peer_steward.peer_message
        real_print = print
        with contextlib.ExitStack() as stack:
            self._native_address_retry_observations(stack)
            inventory = self._listed_rows
            tag = ["3e"]
            stack.enter_context(mock.patch.object(peer_steward, "_prompt_fleet_tag",
                side_effect=lambda _h, sid, **_kw: (tag[0] if sid == "exact-native-thread" else "3e", None)))
            adds, allows, prepared = [], [], []
            def submit(_socket, **kw):
                allows.append(kw["allow_add"])
                if kw["allow_add"]:
                    kw["before_add"]()
                    adds.append({"id": kw["client_message_id"], "thread": kw["thread_id"]})
                    if initial == "ambiguous" and len(adds) == 1:
                        raise queue.QueueDeliveryError("submitted-but-unverified", ambiguous=True)
                return {"status": "queued"}
            stack.enter_context(mock.patch.object(queue, "send_at_least_once", side_effect=submit))
            body = "verified alias sequence " + str((initial, recovery, unresolved, context_kind))
            if context_kind == "long": body += "\ncomplete card and referenced documents " * 180
            body = body.rstrip()
            first = "[3e]" if initial == "ambiguous" else "w1:pX" if recovery == "runner" else "child"
            args = peer_steward.build_parser().parse_args(["prompt", first, body])
            kwargs = {"on_prepared": prepared.append}
            store = peer_steward.peer_obligations.ObligationStore()
            producer_duty = None
            runtime_first = context_kind == "runtime"
            if context_kind != "runtime":
                args.ref = [body[:90]]
            def prompt():
                nonlocal args, kwargs, producer_duty, runtime_first
                if runtime_first:
                    args, kwargs, producer_duty, code = self._canonical_runtime_notice(body, prepared)
                    args.target = first
                    runtime_first = False
                    return code
                if initial == "pending":
                    with mock.patch.object(peer, "deliver_pending_codex", side_effect=OSError("known-unsent")):
                        return peer_steward.cmd_prompt(args, **kwargs)
                return peer_steward.cmd_prompt(args, **kwargs)
            if context_kind in ("short", "long"):
                import session_tidy as tidy
                import session_tidy_runner as runner
                import dispatch_seat_handover as handover
                stack.enter_context(mock.patch.object(runner, "_handoff_steward", return_value=peer_steward))
                stack.enter_context(mock.patch.object(runner, "injected_executable", return_value=None))
                stack.enter_context(mock.patch.object(tidy, "read_latest_card", return_value={
                    "generation": 7, "body": body, "prompt_seq": 0}))
                stack.enter_context(mock.patch.object(tidy, "mark_card_handed_off"))
                stack.enter_context(mock.patch.object(handover, "record_retire_handover", return_value={}))
                stack.enter_context(mock.patch.object(peer_steward, "_retire_foreground", return_value=None))
                evidence = {"schema": "peer-read-access-observation-v1", "observed_at": time.monotonic(),
                    "recipient": {"harness": "codex", "session_id": "exact-native-thread", "pane_id": "w1:pX"},
                    "local_process": {"pid": os.getpid(), "start": "1", "argv": ["codex"], "pid_namespace": "test"}}
                stack.enter_context(mock.patch.object(peer_steward, "_peer_reference_access_evidence", return_value=evidence))
                stack.enter_context(mock.patch.object(peer, "_reference_local_process_matches", return_value=True))
                prior = {r["ref"] for r in peer._pending_rows()}
                # The production typed caller and its on_prepared callback run.
                with mock.patch("builtins.print", side_effect=real_print):
                    _line, code = runner.handoff(tidy.Seat("project", "original-source"), first)
                self.assertEqual(code, 3, _line)
                self.assertIn("prompted=queued", _line)
                original, = [r for r in peer._pending_rows() if r["ref"] not in prior]
                kwargs["handoff"] = original["handoff"]
                args.ref = original["refs"]
                prepared.append(original["ref"])
                self.assertEqual(bool(original.get("body_ref")), context_kind == "long")
                self.assertIn(peer.handoff_accept_command(original["ref"]), original["text"])
            else:
                if unresolved: self._set_inventory([])
                prompt()
                original = peer._read_pending(prepared[-1])
                self._set_inventory(inventory)
                prompt()
            ref = original["ref"]
            original = peer._read_pending(ref)
            wire = peer._transfer_path(ref).read_bytes()
            immutable = {k: original.get(k) for k in (
                "ref", "text", "to", "from", "refs", "handoff", "producer_binding", "body_ref", "body_sha256", "source_sha256")}
            # Observe and use aliases in sequence, including a moved pane/name.
            for alias in ("[3e]", "renamed", "w1:pMoved", "[4f]"):
                if alias != "[3e]":
                    tag[0] = "4f"
                    self._set_inventory([self._inventory_row(harness="codex", sid="exact-native-thread",
                        name="renamed", pane="w1:pMoved")])
                args.target = alias
                prompt()
                self.assertEqual(prepared[-1], ref)
            for _ in range(2):
                self._set_inventory([])
                for alias in (first, "[3e]", "renamed", "w1:pMoved", "[4f]"):
                    args.target = alias
                    before = peer._read_pending(ref)
                    self.assertEqual(peer_steward.cmd_prompt(args, **kwargs), 5)
                    self.assertEqual(peer._read_pending(ref), before)
                    self.assertEqual(prepared[-1], ref)
            # Name/tag ambiguity and reassignment while the original SID is
            # still observable elsewhere cannot acquire another send authority.
            for ambiguous in (False, True):
                tag[0] = "5a"
                rows = [self._inventory_row(harness="codex", sid="other-native", name="renamed"),
                        self._inventory_row(harness="codex", sid="exact-native-thread",
                            name="original moved", pane="w1:pOriginal")]
                if ambiguous:
                    rows.append(self._inventory_row(harness="codex", sid="second-namesake",
                        name="renamed", pane="w1:pOther"))
                self._set_inventory(rows)
                for alias in ("renamed", "[3e]"):
                    args.target = alias
                    count = len(adds)
                    self.assertEqual(peer_steward.cmd_prompt(args, **kwargs), 5)
                    self.assertEqual(prepared[-1], ref)
                    for duty in store.list():
                        if duty["intent"].get("ref") == ref:
                            peer_steward._resume_message_obligation(duty, store)
                    self.assertEqual(len(adds), count)
            self._set_inventory(inventory); tag[0] = "3e"
            args.target = first
            if recovery == "caller":
                peer_steward.cmd_prompt(args, **kwargs)
            elif context_kind == "runtime":
                peer_steward._reconcile_obligations(store, [store.get(producer_duty["id"])])
            else:
                peer_steward._resume_message_obligation(store.get("message-" + ref), store)
            # All observed aliases remain associated even after recovery.
            self._set_inventory([])
            for alias in ("[3e]", "renamed", "w1:pMoved", "[4f]"):
                args.target = alias
                peer_steward.cmd_prompt(args, **kwargs)
                self.assertEqual(prepared[-1], ref)
            latest = peer._read_pending(ref)
            self.assertEqual({k: latest.get(k) for k in immutable}, immutable)
            self.assertEqual(peer._transfer_path(ref).read_bytes(), wire)
            self.assertEqual(peer.pending_delivery_recipient(latest)["session_id"], "exact-native-thread")
            self.assertTrue(all(value == ref for value in prepared))
            self.assertEqual(adds, [{"id": "delivery-peer-" + ref, "thread": "exact-native-thread"}])
            self.assertEqual(len([r for r in peer._pending_rows() if r["source_sha256"] == original["source_sha256"]]), 1)
            if producer_duty:
                current_duty = store.get(producer_duty["id"])
                self.assertEqual(current_duty["intent"], producer_duty["intent"])
                self.assertEqual(current_duty["observation"]["transfer_ref"], ref)
                self.assertIsNone(store.get("message-" + ref))
            else:
                self.assertIsNotNone(store.get("message-" + ref))
            sys.stdout.write("peer-verified-alias=" + json.dumps({"initial": initial, "unresolved_first": unresolved,
                "recovery": recovery, "context": context_kind, "ref": ref, "callbacks": len(prepared),
                "aliases": latest["verified_addresses"], "adds": adds, "allow_add": allows,
                "immutable_preserved": True, "producer_controller_preserved": bool(producer_duty)}) + "\n")

    def test_alias_recording_failure_keeps_the_caller_callback_and_transport_unpublished(self):
        peer = peer_steward.peer_message
        with contextlib.ExitStack() as stack:
            self._native_address_retry_observations(stack)
            stack.enter_context(mock.patch.object(peer_steward, "_prompt_fleet_tag", return_value=("3e", None)))
            stack.enter_context(mock.patch("codex_queue_delivery.send_at_least_once", return_value={"status": "queued"}))
            args = peer_steward.build_parser().parse_args(["prompt", "child", "failed alias record"])
            refs = []
            peer_steward.cmd_prompt(args, on_prepared=refs.append)
            ref, = refs
            args.target = "[3e]"
            for failure in ("write", "read"):
                patch = (mock.patch.object(peer, "_save_pending", side_effect=OSError("address-write-failed"))
                         if failure == "write" else mock.patch.object(peer, "_read_pending", side_effect=OSError("unavailable")))
                with patch, mock.patch.object(peer, "deliver_pending_codex") as send, \
                     mock.patch.object(peer, "_prepare_peer_message") as publish:
                    self.assertEqual(peer_steward.cmd_prompt(args, on_prepared=refs.append), 5)
                    self.assertEqual(refs, [ref])
                    send.assert_not_called(); publish.assert_not_called()
            self.assertEqual(len(list(peer._pending_rows())), 1)
            peer_steward.cmd_prompt(args, on_prepared=refs.append)
            self.assertEqual(refs, [ref, ref])

    def test_retained_typed_handoff_keeps_context_through_unresolved_caller(self):
        peer = peer_steward.peer_message
        with contextlib.ExitStack() as stack:
            self._native_address_retry_observations(stack)
            inventory = self._listed_rows
            adds, prepared = [], []
            def submit(_socket, **kw):
                if kw['allow_add']:
                    kw['before_add']()
                    adds.append(kw['client_message_id'])
                return {'status': 'queued'}
            stack.enter_context(mock.patch('codex_queue_delivery.send_at_least_once', side_effect=submit))
            args = peer_steward.build_parser().parse_args(['prompt', 'child', '[handoff] retained context', '--ref', 'context-ref'])
            context = {'card_id': 'original-card', 'predecessor': {'harness': 'claude', 'session_id': 'original-sender'},
                       'successor': {'harness': 'codex', 'session_id': 'exact-native-thread'}}
            self.assertEqual(peer_steward.cmd_prompt(args, handoff=context, on_prepared=prepared.append), 3)
            ref, = prepared
            original = peer._read_pending(ref)
            self._set_inventory([])
            for _ in range(3):
                self.assertEqual(peer_steward.cmd_prompt(args, handoff=context, on_prepared=prepared.append), 5)
                self.assertEqual(prepared[-1], ref)
                self.assertEqual(peer._read_pending(ref), original)
            self.assertEqual(prepared, [ref] * 4)
            self.assertEqual(len(list(peer._pending_rows())), 1)
            self.assertEqual(len(peer_steward.peer_obligations.ObligationStore().list()), 1)
            # A genuinely different context is a different request, even during outage.
            peer_steward.cmd_prompt(args, handoff=dict(context, card_id='other-card'), on_prepared=prepared.append)
            self.assertNotEqual(prepared[-1], ref)
            self._set_inventory(inventory)
            peer_steward.cmd_prompt(args, handoff=context, on_prepared=prepared.append)
            self.assertEqual(prepared[-1], ref)
            self.assertEqual(adds, ['delivery-peer-' + ref])

    def test_retained_runtime_producer_keeps_its_original_controller_through_outage(self):
        peer = peer_steward.peer_message
        with contextlib.ExitStack() as stack:
            self._native_address_retry_observations(stack)
            inventory = self._listed_rows
            store = peer_steward.peer_obligations.ObligationStore()
            adds, prepared = [], []
            def submit(_socket, **kwargs):
                if kwargs["allow_add"]:
                    kwargs["before_add"]()
                    adds.append(kwargs["client_message_id"])
                return {"status": "queued"}
            stack.enter_context(mock.patch("codex_queue_delivery.send_at_least_once", side_effect=submit))
            args, kwargs, duty, code = self._canonical_runtime_notice("runtime retained context", prepared)
            self.assertEqual(code, 3)
            ref, = prepared
            original = peer._read_pending(ref)
            intent = dict(duty["intent"])
            wire = peer._transfer_path(ref).read_bytes()
            self._set_inventory([])
            for _ in range(3):
                self.assertEqual(peer_steward.cmd_prompt(args, **kwargs), 5)
                self.assertEqual(prepared[-1], ref)
                self.assertEqual(peer._read_pending(ref), original)
            self.assertEqual(prepared, [ref] * 4)
            self.assertEqual(store.get(duty["id"])["intent"], intent)
            self.assertEqual(len(store.list()), 1)
            self._set_inventory([self._inventory_row(harness="codex", sid="other-native"),
                self._inventory_row(harness="codex", sid="exact-native-thread", name="original moved", pane="w1:pOriginal")])
            self.assertEqual(peer_steward.cmd_prompt(args, **kwargs), 5)
            self.assertEqual(prepared[-1], ref)
            self.assertEqual(adds, ["delivery-peer-" + ref])
            self._set_inventory(inventory)
            peer_steward.cmd_prompt(args, **kwargs)
            self.assertEqual(prepared[-1], ref)
            self.assertEqual(adds, ["delivery-peer-" + ref])
            bad = json.loads(json.dumps(kwargs["producer_binding"]))
            bad["records"][0]["receipt_digest"] = "sha256:" + "c" * 64
            before = len(prepared)
            peer_steward.cmd_prompt(args, **{**kwargs, "producer_binding": bad})
            self.assertEqual(len(prepared), before)
            self.assertEqual(adds, ["delivery-peer-" + ref])
            self.assertEqual(peer._transfer_path(ref).read_bytes(), wire)
            self.assertEqual(store.get(duty["id"])["intent"], intent)
            self.assertEqual(len(store.list()), 1)

    def test_concurrent_exact_outage_retries_reuse_the_original_duty(self):
        import codex_queue_delivery as queue
        peer = peer_steward.peer_message
        with contextlib.ExitStack() as stack:
            self._native_address_retry_observations(stack)
            inventory = self._listed_rows
            args = peer_steward.build_parser().parse_args(["prompt", "child", "concurrent exact outage"])
            with mock.patch.object(peer, "deliver_pending_codex", side_effect=OSError("queue-outage")):
                self.assertEqual(peer_steward.cmd_prompt(args), 5)
            store = peer_steward.peer_obligations.ObligationStore()
            duty, = store.list()
            original = peer._read_pending(duty["intent"]["ref"])
            self._set_inventory([])
            entered, release = threading.Event(), threading.Event()
            read = peer._pending_rows
            def paused_read(**kw):
                entered.set()
                self.assertTrue(release.wait(5))
                yield from read(**kw)
            codes, errors = [], []
            def retry():
                try: codes.append(peer_steward.cmd_prompt(args))
                except BaseException as exc: errors.append(exc)
            with mock.patch.object(peer, "_pending_rows", side_effect=paused_read):
                thread = threading.Thread(target=retry)
                thread.start()
                try:
                    self.assertTrue(entered.wait(5))
                    self.assertEqual(peer_steward.cmd_prompt(args), 5)
                finally:
                    release.set()
                    thread.join(5)
            self.assertFalse(thread.is_alive())
            self.assertFalse(errors)
            self.assertEqual(codes, [5])
            self.assertEqual(list(peer._pending_rows()), [original])
            self.assertEqual(store.list(), [duty])
            self._set_inventory(inventory)
            adds = []
            def submit(_socket, **kw):
                if kw["allow_add"]:
                    kw["before_add"]()
                    adds.append(kw["client_message_id"])
                return {"status": "queued"}
            with mock.patch.object(queue, "send_at_least_once", side_effect=submit):
                peer_steward.cmd_prompt(args)
                peer_steward._resume_message_obligation(store.get(duty["id"]), store)
            self.assertEqual(adds, ["delivery-peer-" + original["ref"]])

    def test_concurrent_unresolved_prompt_retries_retain_one_duty_and_submit_once(self):
        import codex_queue_delivery as queue
        peer = peer_steward.peer_message
        with contextlib.ExitStack() as stack:
            self._native_address_retry_observations(stack)
            saved_rows = self._listed_rows
            self._set_inventory([])
            args = peer_steward.build_parser().parse_args([
                "prompt", "child", "concurrent caller correction", "--ref", "stable-concurrent-request"])
            entered, release = threading.Event(), threading.Event()
            original_prepare = peer._prepare_peer_message
            codes, errors = [], []
            def delayed(*args, **kwargs):
                entered.set()
                if not release.wait(5): raise AssertionError("preparation not released")
                return original_prepare(*args, **kwargs)
            def first():
                try: codes.append(peer_steward.cmd_prompt(args))
                except BaseException as exc: errors.append(exc)
            with mock.patch.object(peer, "_prepare_peer_message", side_effect=delayed):
                thread = threading.Thread(target=first)
                thread.start()
                try:
                    self.assertTrue(entered.wait(5))
                    self.assertEqual(peer_steward.cmd_prompt(args), 5)
                finally:
                    release.set()
                    thread.join(5)
                    self.assertFalse(thread.is_alive())
            self.assertFalse(errors)
            self.assertEqual(codes, [5])
            self.assertEqual(peer_steward.cmd_prompt(args), 5)
            store = peer_steward.peer_obligations.ObligationStore()
            duty, = [d for d in store.list() if d["intent"]["kind"] == "message"]
            row, = list(peer._pending_rows())
            self.assertEqual(duty["intent"]["ref"], row["ref"])
            self._set_inventory(saved_rows)
            adds = []
            def submit(_socket, **kwargs):
                kwargs["before_add"]()
                adds.append(kwargs["client_message_id"])
                return {"status": "queued"}
            with mock.patch.object(queue, "send_at_least_once", side_effect=submit):
                peer_steward._resume_message_obligation(duty, store)
                peer_steward._resume_message_obligation(store.get(duty["id"]), store)
            self.assertEqual(adds, ["delivery-peer-" + row["ref"]])

    def test_message_recovery_scopes_alternating_endpoints_before_binding_and_restores_on_returns(self):
        import codex_queue_delivery as queue
        peer = peer_steward.peer_message
        with contextlib.ExitStack() as stack:
            self._native_address_retry_observations(stack)
            stack.enter_context(mock.patch.object(peer, "_promote_prior_codex_rows", return_value=[]))
            endpoints = {server: [self._inventory_row(harness="codex", sid=server + "-thread")]
                         for server in ("selected", "ambient")}
            observed = []
            def inventory():
                server = peer_steward._HERDR_SESSION
                observed.append(server)
                return (endpoints[server], None) if endpoints[server] is not None else ([], "target-observation-unavailable")
            def census(session=None):
                server = session or "default"
                return SimpleNamespace(endpoint=server, complete=endpoints.get(server) is not None,
                    observed_at=time.time(), panes=tuple(endpoints.get(server) or []))
            def target(pane):
                rows = endpoints[peer_steward._HERDR_SESSION] or []
                row = next((r for r in rows if r["pane_id"] == pane), None)
                if not row:
                    return "unknown", {}, 2, "target-observation-unavailable"
                return self._current_target("codex", row["agent_session"]["value"], "child", pane)
            stack.enter_context(mock.patch.object(peer_steward, "_prompt_agent_inventory", side_effect=inventory))
            stack.enter_context(mock.patch.object(peer_steward, "_retire_target", side_effect=target))
            stack.enter_context(mock.patch.object(peer_steward.herdr_observation, "pane_census", side_effect=census))
            submissions = []
            def submit(_socket, **kwargs):
                kwargs["before_add"]()
                submissions.append((peer_steward._HERDR_SESSION, kwargs["thread_id"]))
                return {"status": "queued"}
            stack.enter_context(mock.patch.object(queue, "send_at_least_once", side_effect=submit))
            store = peer_steward.peer_obligations.ObligationStore()
            duties = []
            for index, server in enumerate(("selected", "ambient", "selected")):
                peer_steward._HERDR_SESSION = server
                _text, ref = peer.prepare_peer_message("endpoint recovery " + str(index),
                    {"harness": "claude", "session_id": "sender"},
                    {"harness": "unknown", "session_id": None, "name": "child"},
                    defer=True, original_target="child", server=server)
                duties.append(peer_steward._schedule_message_obligation(peer._read_pending(ref), "child"))
            # An unavailable selected endpoint cannot borrow ambient's namesake.
            selected = endpoints["selected"]
            endpoints["selected"] = None
            peer_steward._HERDR_SESSION = "ambient"
            peer_steward._resume_message_obligation(duties[0], store)
            self.assertEqual(peer_steward._HERDR_SESSION, "ambient")
            self.assertIsNone(peer._read_pending(duties[0]["intent"]["ref"]).get("bound_to"))
            self.assertNotEqual(store.get(duties[0]["id"])["state"], "cancelled")
            endpoints["selected"] = selected
            endpoints["selected"] = selected + [self._inventory_row(
                harness="codex", sid="another-thread", pane="w1:pOther")]
            peer_steward._resume_message_obligation(store.get(duties[0]["id"]), store)
            self.assertIsNone(peer._read_pending(duties[0]["intent"]["ref"]).get("bound_to"))
            self.assertFalse(submissions)
            self.assertEqual(peer_steward._HERDR_SESSION, "ambient")
            endpoints["selected"] = selected
            with mock.patch.object(peer_steward, "_prompt_process_session", return_value=("actual-other", None)):
                peer_steward._resume_message_obligation(store.get(duties[0]["id"]), store)
            self.assertIsNone(peer._read_pending(duties[0]["intent"]["ref"]).get("bound_to"))
            self.assertFalse(submissions)
            self.assertEqual(peer_steward._HERDR_SESSION, "ambient")
            for duty in duties:
                peer_steward._HERDR_SESSION = "ambient"
                peer_steward._resume_message_obligation(store.get(duty["id"]), store)
                server = duty["intent"]["identity"]["server"]
                row = peer._read_pending(duty["intent"]["ref"])
                self.assertEqual(row["bound_to"]["session_id"], server + "-thread")
                self.assertEqual(peer_steward._HERDR_SESSION, "ambient")
                peer_steward._resume_message_obligation(store.get(duty["id"]), store)
                self.assertEqual(peer_steward._HERDR_SESSION, "ambient")
            self.assertEqual(submissions, [(s, s + "-thread") for s in ("selected", "ambient", "selected")])
            # Unexpected errors and the missing-row early return also restore scope.
            with mock.patch.object(peer, "_read_pending", side_effect=OSError("unreadable")):
                peer_steward._resume_message_obligation(duties[0], store)
            self.assertEqual(peer_steward._HERDR_SESSION, "ambient")
            with mock.patch.object(peer, "_read_pending", side_effect=RuntimeError("interrupted")):
                with self.assertRaises(RuntimeError):
                    peer_steward._resume_message_obligation(duties[0], store)
            self.assertEqual(peer_steward._HERDR_SESSION, "ambient")
            self.assertIn("selected", observed)
            sys.stdout.write("peer-endpoint-probe=" + json.dumps({"submissions": submissions,
                "ambient_restored": peer_steward._HERDR_SESSION, "outage_ambiguity_other_sid_unsent": True}) + "\n")

    def test_native_retry_rechecks_actual_other_sid_at_both_effect_boundaries(self):
        peer = peer_steward.peer_message
        for boundary in (1, 2):
            with self.subTest(boundary=boundary), contextlib.ExitStack() as stack:
                process = self._native_address_retry_observations(stack)
                body = "pinned native correction " + str(boundary)
                args = peer_steward.build_parser().parse_args(["prompt", "child", body])
                with mock.patch.object(peer, "deliver_pending_codex", return_value={"status": "retryable"}):
                    self.assertEqual(peer_steward.cmd_prompt(args), 5)
                store = peer_steward.peer_obligations.ObligationStore()
                duties = [d for d in store.list() if d["intent"].get("ref") == self._all_records()[-1]["transfer_ref"]]
                self.assertEqual(len(duties), 1)
                duty, = duties
                original = peer._read_pending(duty["intent"]["ref"])
                exact = peer_steward._prompt_exact_candidate
                checks = []
                def observe(*args, **kwargs):
                    checks.append(1)
                    if len(checks) == boundary:
                        process.return_value = ("actual-other-thread", None)
                    return exact(*args, **kwargs)
                with mock.patch.object(peer_steward, "_prompt_exact_candidate", side_effect=observe), \
                     mock.patch.object(peer, "deliver_pending_codex") as send, \
                     mock.patch.object(peer_steward, "_herdr_prompt") as physical:
                    peer_steward._resume_message_obligation(duty, store)
                send.assert_not_called()
                physical.assert_not_called()
                self.assertEqual(len(checks), boundary)
                self.assertEqual(store.get(duty["id"])["observation"]["reason"], "target-identity-changed")
                self.assertEqual(peer._read_pending(duty["intent"]["ref"])["text"], original["text"])

    def test_ambiguous_native_submission_duty_is_receive_only_without_process_sid(self):
        import codex_queue_delivery as queue
        peer = peer_steward.peer_message
        with contextlib.ExitStack() as stack:
            self._native_address_retry_observations(stack)
            def ambiguous(_socket, **kwargs):
                kwargs["before_add"]()
                raise queue.QueueDeliveryError("submitted-but-unverified", ambiguous=True)
            send = stack.enter_context(mock.patch.object(queue, "send_at_least_once", side_effect=ambiguous))
            args = peer_steward.build_parser().parse_args(["prompt", "child", "ambiguous original"])
            self.assertEqual(peer_steward.cmd_prompt(args), 5)
            store = peer_steward.peer_obligations.ObligationStore()
            duty, = [d for d in store.list() if d["intent"]["kind"] == "message"]
            original = peer._read_pending(duty["intent"]["ref"])
            self.assertEqual(original["state"], "unverified")
            peer_steward._resume_message_obligation(duty, store)
            peer_steward._resume_message_obligation(store.get(duty["id"]), store)
            self.assertEqual(send.call_count, 1)
            self.assertEqual(peer._read_pending(duty["intent"]["ref"])["body_sha256"], original["body_sha256"])

    def test_native_endpoint_without_process_sid_is_distinct_from_input_proof(self):
        ident = {"harness": "codex", "session_id": "native-thread", "pane": "w1:pX", "name": "peer"}
        candidate, error = peer_steward._prompt_current_candidate(
            "w1:pX", expected_sid="native-thread", require_process_session=True,
            target_observation=("idle", ident), pane_info=self._pane_info_for("w1:pX"),
            native_address=("codex", "native-thread", "w1:pX"))
        self.assertIsNone(error)
        self.assertEqual(candidate["session_id"], "native-thread")
        self.assertIsNone(candidate["_native_session_id"])

    def test_name_tag_and_pane_addresses_pin_the_same_current_recipient(self):
        row = self._inventory_row()
        self._set_inventory([row])
        self._native_sid_by_pid[40000] = "sid-child"
        target = self._current_target()
        with mock.patch.object(peer_steward, "_prompt_agent_inventory",
                               return_value=([row], None)), \
             mock.patch.object(peer_steward, "_retire_target", return_value=target), \
             mock.patch.object(peer_steward, "_retire_pane_info", side_effect=self._pane_info_for), \
             mock.patch.object(peer_steward, "_prompt_fleet_tag", return_value=("3e", None)):
            resolved = [peer_steward._resolve_prompt_target(address)
                        for address in ("child", "[3e]", "w1:pX")]
        identities = [(item[0]["harness"], item[0]["session_id"], item[0]["pane"])
                      for item in resolved]
        self.assertEqual(identities, [("claude", "sid-child", "w1:pX")] * 3)
        self.assertTrue(all(item[1] is None for item in resolved))

    def test_unnamed_herdr_row_does_not_hide_an_exact_named_recipient(self):
        rows = [self._inventory_row(), self._inventory_row(
            harness="claude", sid="sid-unnamed", name=None, pane="w1:pU")]
        self._set_inventory(rows)
        with mock.patch.object(peer_steward, "_prompt_agent_inventory",
                               return_value=(rows, None)), \
             mock.patch.object(peer_steward, "_retire_target", side_effect=self._target_for), \
             mock.patch.object(peer_steward, "_retire_pane_info", side_effect=self._pane_info_for):
            resolved = peer_steward._resolve_prompt_target("child")
        self.assertIsNone(resolved[1])
        self.assertEqual((resolved[0]["harness"], resolved[0]["session_id"],
                          resolved[0]["pane"]), ("claude", "sid-child", "w1:pX"))

    def test_unrelated_unknown_identity_does_not_block_exact_name_or_tag(self):
        rows = [self._inventory_row(), self._inventory_row(
            harness="claude", sid="sid-unrelated", name="other", pane="w1:pU")]
        self._set_inventory(rows)
        self._native_sid_by_pid.pop(40001, None)
        self._native_sid_by_harness["claude"] = ""
        with mock.patch.object(peer_steward, "_prompt_agent_inventory",
                               return_value=(rows, None)), \
             mock.patch.object(peer_steward, "_retire_target", side_effect=self._target_for), \
             mock.patch.object(peer_steward, "_retire_pane_info", side_effect=self._pane_info_for), \
             mock.patch.object(peer_steward, "_prompt_process_session",
                               wraps=peer_steward._prompt_process_session) as process_session, \
             mock.patch.object(peer_steward, "_prompt_fleet_tag",
                               side_effect=lambda _harness, sid, **_kw:
                               ({"sid-child": "3e", "sid-unrelated": "7a"}.get(sid), None)):
            named = peer_steward._resolve_prompt_target("child")
            tagged = peer_steward._resolve_prompt_target("[3e]")
        self.assertIsNone(named[1])
        self.assertIsNone(tagged[1])
        self.assertEqual((named[0]["pane"], tagged[0]["pane"]), ("w1:pX", "w1:pX"))
        self.assertEqual(process_session.call_count, 4,
                         "only the matching pane needs native identity proof per address")

        self._native_sid_by_pid.pop(40000, None)
        with mock.patch.object(peer_steward, "_prompt_agent_inventory",
                               return_value=(rows, None)), \
             mock.patch.object(peer_steward, "_retire_target", side_effect=self._target_for), \
             mock.patch.object(peer_steward, "_retire_pane_info", side_effect=self._pane_info_for):
            uncertain = peer_steward._resolve_prompt_target("child")
        self.assertEqual(uncertain[0]["session_id"], "sid-child")
        self.assertIsNone(uncertain[0]["_native_session_id"])
        self.assertIsNone(uncertain[1])

    def test_native_identity_can_discover_a_registry_name_when_census_has_no_name(self):
        row = self._inventory_row(sid=None, name=None)
        self._set_inventory([row])
        self._census_panes[0].pop("agent_session", None)
        self._native_sid_by_pid[40000] = "sid-native-only"
        target = ("idle", {"harness": "claude", "session_id": "-",
                            "name": None, "pane": "w1:pX"}, 0, None)
        with mock.patch.object(peer_steward, "_prompt_agent_inventory",
                               return_value=([row], None)), \
             mock.patch.object(peer_steward, "_retire_target", return_value=target), \
             mock.patch.object(peer_steward, "_retire_pane_info", side_effect=self._pane_info_for), \
             mock.patch.object(peer_steward, "_from_name", return_value="registry-only"):
            resolved = peer_steward._resolve_prompt_target("registry-only")
        self.assertIsNone(resolved[1])
        self.assertEqual((resolved[0]["session_id"], resolved[0]["pane"]),
                         ("sid-native-only", "w1:pX"))

    def test_same_pane_duplicate_observation_merges_optional_name_and_identity(self):
        rows = [self._inventory_row(sid=None, name=None), self._inventory_row()]
        collapsed = peer_steward._prompt_collapse_rows([
            peer_steward._prompt_inventory_fields(row) for row in rows])
        self.assertEqual(len(collapsed), 1)
        self.assertEqual((collapsed[0]["name"], collapsed[0]["session_id"]),
                         ("child", "sid-child"))

    def test_duplicate_name_and_tag_candidates_return_every_pane_without_selecting(self):
        rows = [self._inventory_row(pane="w1:pA"), self._inventory_row(pane="w1:pB")]
        self._set_inventory(rows)
        with mock.patch.object(peer_steward, "_prompt_agent_inventory",
                               return_value=(rows, None)), \
             mock.patch.object(peer_steward, "_retire_target", side_effect=self._target_for), \
             mock.patch.object(peer_steward, "_retire_pane_info", side_effect=self._pane_info_for), \
             mock.patch.object(peer_steward, "_prompt_fleet_tag", return_value=("3e", None)), \
             mock.patch.object(peer_steward, "_prompt_process_session",
                               wraps=peer_steward._prompt_process_session) as current:
            name_result = peer_steward._resolve_prompt_target("child")
            tag_result = peer_steward._resolve_prompt_target("[3e]")
        self.assertEqual(name_result[1:], ("target-name-ambiguous", ["w1:pA", "w1:pB"]))
        self.assertEqual(tag_result[1:], ("fleet-tag-ambiguous", ["w1:pA", "w1:pB"]))
        self.assertEqual(current.call_count, 4)

    def test_bracket_tag_that_is_also_another_panes_name_is_ambiguous(self):
        rows = [self._inventory_row(harness="claude", sid="sid-a", name="alpha",
                                    pane="w1:pA"),
                self._inventory_row(harness="codex", sid="sid-b", name="[3e]",
                                    pane="w1:pB")]
        self._set_inventory(rows)
        with mock.patch.object(peer_steward, "_prompt_agent_inventory",
                               return_value=(rows, None)), \
             mock.patch.object(peer_steward, "_retire_target", side_effect=self._target_for), \
             mock.patch.object(peer_steward, "_retire_pane_info", side_effect=self._pane_info_for), \
             mock.patch.object(peer_steward, "_prompt_fleet_tag",
                               side_effect=lambda harness, sid, **_kw:
                               ("3e" if sid == "sid-a" else "7a", None)):
            result = peer_steward._resolve_prompt_target("[3e]")
        self.assertEqual(result[1:], ("fleet-tag-ambiguous", ["w1:pA", "w1:pB"]))

    def _resolve_bare(self, rows, tags, addresses):
        self._set_inventory(rows)
        with mock.patch.object(peer_steward, "_prompt_agent_inventory",
                               return_value=(rows, None)), \
             mock.patch.object(peer_steward, "_retire_target", side_effect=self._target_for), \
             mock.patch.object(peer_steward, "_retire_pane_info", side_effect=self._pane_info_for), \
             mock.patch.object(peer_steward, "_prompt_fleet_tag",
                               side_effect=lambda _harness, sid, **_kw: (tags.get(sid), None)):
            return [peer_steward._resolve_prompt_target(address) for address in addresses]

    def test_bare_fleet_number_resolves_as_tag_only_when_no_name_matches(self):
        # Agents and reports call sessions by their Fleet number without brackets
        # (W7, 11-03: three `prompt e8` failures with target-name-not-found).
        rows = [self._inventory_row(sid="sid-a", name="alpha", pane="w1:pA")]
        lower, upper = self._resolve_bare(rows, {"sid-a": "3e"}, ("3e", "3E"))
        for result in (lower, upper):
            self.assertIsNone(result[1])
            self.assertEqual((result[0]["session_id"], result[0]["pane"]), ("sid-a", "w1:pA"))

    def test_bare_fleet_number_keeps_name_precedence_and_ambiguity(self):
        named = self._resolve_bare(
            [self._inventory_row(sid="sid-a", name="3e", pane="w1:pA"),
             self._inventory_row(harness="codex", sid="sid-b", name="beta", pane="w1:pB")],
            {"sid-a": "7a", "sid-b": "3e"}, ("3e",))[0]
        self.assertIsNone(named[1])
        self.assertEqual(named[0]["pane"], "w1:pA", "an exact name wins over a Fleet tag")
        ambiguous = self._resolve_bare(
            [self._inventory_row(sid="sid-a", name="alpha", pane="w1:pA"),
             self._inventory_row(harness="codex", sid="sid-b", name="beta", pane="w1:pB")],
            {"sid-a": "3e", "sid-b": "3e"}, ("3e",))[0]
        self.assertEqual(ambiguous[1:], ("fleet-tag-ambiguous", ["w1:pA", "w1:pB"]))
        missing = self._resolve_bare(
            [self._inventory_row(sid="sid-a", name="alpha", pane="w1:pA")],
            {"sid-a": "7a"}, ("3e",))[0]
        self.assertEqual(missing[1:], ("fleet-tag-not-found", []))

    def test_bare_fleet_number_does_not_fall_back_past_an_uncertain_name_stage(self):
        row = self._inventory_row(sid="sid-a", name="alpha", pane="w1:pA")
        self._set_inventory([row])
        with mock.patch.object(peer_steward.herdr_observation, "pane_census", return_value=None), \
             mock.patch.object(peer_steward, "_prompt_agent_inventory",
                               return_value=([row], None)), \
             mock.patch.object(peer_steward, "_prompt_fleet_tag",
                               return_value=("3e", None)) as tag:
            result = peer_steward._resolve_prompt_target("3e")
        self.assertEqual(result[1:], ("pane-census-unavailable", ["w1:pA"]))
        tag.assert_not_called()

    def test_non_fleet_number_shapes_stay_names(self):
        rows = [self._inventory_row(sid="sid-a", name="alpha", pane="w1:pA")]
        results = self._resolve_bare(rows, {"sid-a": "3e"}, ("3g", "abc", "[3e", "3e]"))
        self.assertEqual([result[1:] for result in results],
                         [("target-name-not-found", [])] * 4)

    def test_prompt_process_identity_rejects_conflicting_foreground_claims(self):
        self._native_sid_by_pid.update({51001: "sid-first", 51002: "sid-second"})
        info = {"pane_id": "w1:pX", "foreground_processes": [
            {"pid": 51001}, {"pid": 51002}]}
        self.assertEqual(peer_steward._prompt_process_session("w1:pX", "claude", info),
                         (None, "target-identity-changed"))

    def test_prompt_name_lookup_includes_current_registry_and_fleet_pane_names(self):
        row = self._inventory_row(name="herdr-child")
        self._set_inventory([row])
        self._census_panes[0].update(title="Fleet pane title", terminal_title_stripped="Terminal title")
        self._census_panes[0]["display_agent"] = "[3e] claude"
        self._native_sid_by_pid[40000] = "sid-child"

        def names(harness, sid):
            return "registry-display-name" if sid == "sid-child" else None

        with mock.patch.object(peer_steward, "_prompt_agent_inventory",
                               return_value=([row], None)), \
             mock.patch.object(peer_steward, "_retire_target", side_effect=self._target_for), \
             mock.patch.object(peer_steward, "_retire_pane_info", side_effect=self._pane_info_for), \
             mock.patch.object(peer_steward, "_from_name", side_effect=names):
            results = [peer_steward._resolve_prompt_target(name)
                       for name in ("herdr-child", "registry-display-name", "Fleet pane title",
                                    "Terminal title", "[3e] claude")]
        self.assertTrue(all(result[1] is None for result in results))
        self.assertEqual({(result[0]["harness"], result[0]["session_id"], result[0]["pane"])
                          for result in results}, {("claude", "sid-child", "w1:pX")})

    def test_failed_pane_census_keeps_agent_list_candidate_panes(self):
        row = self._inventory_row()
        self._set_inventory([row])
        with mock.patch.object(peer_steward.herdr_observation, "pane_census", return_value=None), \
             mock.patch.object(peer_steward, "_prompt_agent_inventory",
                               return_value=([row], None)):
            result = peer_steward._resolve_prompt_target("child")
        self.assertEqual(result[1:], ("pane-census-unavailable", ["w1:pX"]))

    def test_missing_name_or_tag_and_failed_inventory_report_distinct_reasons(self):
        self._set_inventory([])
        with mock.patch.object(peer_steward, "_prompt_agent_inventory",
                               return_value=([], None)):
            name_result = peer_steward._resolve_prompt_target("absent-peer")
            tag_result = peer_steward._resolve_prompt_target("[ff]")
        self.assertEqual(name_result[1:], ("target-name-not-found", []))
        self.assertEqual(tag_result[1:], ("fleet-tag-not-found", []))
        with mock.patch.object(peer_steward, "_prompt_agent_inventory",
                               return_value=(None, "herdr-agent-list-unavailable")):
            unavailable = peer_steward._resolve_prompt_target("absent-peer")
        self.assertEqual(unavailable[1:], ("herdr-agent-list-unavailable", []))

    def test_tag_resolution_and_identity_observation_fail_closed(self):
        rows = [self._inventory_row(pane="w1:pX"), self._inventory_row(
            harness="codex", sid="thread-other", name="other", pane="w1:pY")]
        self._set_inventory(rows)
        self._native_sid_by_pid.pop(40000, None)
        self._native_sid_by_harness["claude"] = ""
        with mock.patch.object(peer_steward, "_prompt_agent_inventory",
                               return_value=(rows, None)), \
             mock.patch.object(peer_steward, "_retire_target", side_effect=self._target_for) as current, \
             mock.patch.object(peer_steward, "_retire_pane_info", side_effect=self._pane_info_for), \
             mock.patch.object(peer_steward, "_prompt_fleet_tag", return_value=("3e", None)):
            result = peer_steward._resolve_prompt_target("[3e]")
        self.assertEqual(result[1], "fleet-tag-ambiguous")
        self.assertEqual(result[2], ["w1:pX", "w1:pY"])
        self.assertEqual(current.call_count, 2)

        replacement = self._current_target(sid="sid-new")
        self._set_inventory([self._inventory_row()])
        self._native_sid_by_pid[40000] = "sid-new"
        with mock.patch.object(peer_steward, "_prompt_agent_inventory",
                               return_value=([self._inventory_row()], None)), \
             mock.patch.object(peer_steward, "_retire_target", return_value=replacement), \
             mock.patch.object(peer_steward, "_retire_pane_info", side_effect=self._pane_info_for):
            result = peer_steward._resolve_prompt_target("child")
        self.assertEqual(result[1:], ("target-identity-changed", ["w1:pX"]))

    def test_ambiguous_prompt_returns_candidates_before_creating_or_sending(self):
        rows = [self._inventory_row(pane="w1:pA"), self._inventory_row(pane="w1:pB")]
        self._set_inventory(rows)
        args = peer_steward.build_parser().parse_args(["prompt", "child", "do not guess"])
        with mock.patch.object(peer_steward.shutil, "which", return_value="/usr/bin/herdr"), \
             mock.patch.object(peer_steward, "_prompt_agent_inventory",
                               return_value=(rows, None)), \
             mock.patch.object(peer_steward, "_retire_target", side_effect=self._target_for), \
             mock.patch.object(peer_steward, "_retire_pane_info", side_effect=self._pane_info_for), \
             mock.patch.object(peer_steward, "_current_session_identity",
                               return_value=("sid-steward", "claude")), \
             mock.patch.object(peer_steward, "_from_name", return_value="steward"), \
             mock.patch.object(peer_steward, "_project_of", return_value="/fixture"), \
             mock.patch.object(peer_steward.peer_message, "prepare_peer_message") as prepare, \
             mock.patch.object(peer_steward, "_herdr_prompt") as send, \
             mock.patch("builtins.print") as printed:
            rc = peer_steward.cmd_prompt(args)
        self.assertEqual(rc, 5)
        self.assertIn("prompted=unverified", printed.call_args[0][0])
        prepare.assert_called_once()
        send.assert_not_called()

    def test_opencode_sid_prompt_uses_the_same_guarded_native_input_as_claude(self):
        """An exact-SID OpenCode row takes Claude's guarded path; without process
        proof it still sends no input and keeps the row pending (rc 3)."""
        os.environ["CLAUDE_CODE_SESSION_ID"] = "sid-steward"
        os.environ["HERDR_PANE_ID"] = "w:sender"
        harness = "opencode"
        calls = []
        payload = _agent_json(harness, "sid-" + harness, "child", status="idle")
        with mock.patch.object(peer_steward.shutil, "which", return_value="/usr/bin/herdr"), \
             mock.patch.object(peer_steward.subprocess, "run",
                               side_effect=self._fake_run(payload, calls)), \
             mock.patch.object(peer_steward, "_schedule_message_obligation"), \
             mock.patch("builtins.print") as printed:
            rc = peer_steward.main(["prompt", "child", "[steer] deliver once"])
        self.assertEqual(rc, 3)
        self.assertIn("prompted=queued", printed.call_args[0][0])
        self.assertEqual(self._pane_io_calls(calls), [])
        row = peer_steward.peer_message._read_pending(self._all_records()[-1]["transfer_ref"])
        self.assertEqual(row["state"], "pending")
        self.assertEqual(row["from"]["pane_id"], "w:sender")
        self.assertIn(" ; pane=w:sender ; sid=sid-steward", row["text"])
        self.assertEqual(row["to"]["session_id"], "sid-opencode")
        self.assertTrue(peer_steward.peer_message.parse_peer_trailer(
            row["text"], row["to"], include_ref=True))

    def test_unproven_sender_pane_is_not_borrowed_from_recipient_or_environment(self):
        import pane_ownership
        with mock.patch.dict(os.environ, {"HERDR_PANE_ID": "w:inherited"}), \
             mock.patch.object(pane_ownership, "verified_pane", return_value=""):
            rc, _printed, row, _sent, _schedule = self._run_claude_prompt(state="busy")
        self.assertNotIn("pane_id", row["from"])
        self.assertIn(" ; pane=- ; sid=sid-steward", row["text"])
        self.assertEqual(row["to"]["pane_id"], "w1:pX")

    def _run_claude_prompt(self, *, state="idle", screen=None,
                           consume=False, readiness=None, foregrounds=None,
                           send_result=(0, {}), producer_binding=None,
                           body="[steer] exact private recipient ref", on_prepared=None):
        if screen is None:
            screen = CLAUDE_SUGGESTION
        os.environ["CLAUDE_CODE_SESSION_ID"] = "sid-steward"
        ident = {"harness": "claude", "session_id": "sid-child",
                 "name": "child", "pane": "w1:pX"}
        row = self._inventory_row()
        self._set_inventory([row])
        self._native_sid_by_pid[40000] = "sid-child"
        foreground = {"pid": os.getpid(), "start": "fixture-start",
                      "pid_namespace": os.readlink("/proc/self/ns/pid"),
                      "session_id": "sid-child"}
        snapshots = iter(foregrounds or [foreground, foreground])
        sent = []
        def deliver(target, text, *, wait, timeout_ms, start_aware=False):
            sent.append((target, text, wait, timeout_ms))
            if consume:
                self.assertEqual(peer_steward.peer_message.receive_peer_message(
                    text, {"harness": "claude", "session_id": "sid-child"}), 0)
            return send_result
        ready = readiness or SimpleNamespace(state="ready", reason="")
        screen_reader = mock.Mock(return_value=peer_steward._screen_lines(screen))
        if state != "idle":
            screen_reader.side_effect = AssertionError("busy/blocked target screen must not be read")
        with mock.patch.object(peer_steward.shutil, "which", return_value="/usr/bin/herdr"), \
             mock.patch.object(peer_steward, "_current_session_identity",
                               return_value=("sid-steward", "claude")), \
             mock.patch.object(peer_steward, "_from_name", return_value="steward"), \
             mock.patch.object(peer_steward, "_project_of", return_value="/fixture"), \
             mock.patch.object(peer_steward, "_resolve_target",
                               return_value=("claude", "sid-child", "child")), \
             mock.patch.object(peer_steward, "_prompt_agent_inventory",
                               return_value=([row], None)), \
             mock.patch.object(peer_steward, "_retire_pane_info", side_effect=self._pane_info_for), \
             mock.patch.object(peer_steward, "_agent_state",
                               return_value=(state, "w1:pX")), \
             mock.patch.object(peer_steward, "_retire_target",
                               return_value=(state, ident, 0, None)), \
             mock.patch.object(peer_steward, "_pane_readiness", return_value=ready), \
             mock.patch.object(peer_steward, "_prompt_foreground_proof",
                               side_effect=lambda *_a, **_k: next(snapshots)), \
             mock.patch.object(peer_steward, "_read_screen", screen_reader), \
             mock.patch.object(peer_steward, "_herdr_prompt", side_effect=deliver), \
             mock.patch.object(peer_steward, "_schedule_message_obligation") as schedule, \
             mock.patch.object(peer_steward.peer_obligations, "ensure_normal_runner",
                               return_value=False), \
             mock.patch("builtins.print") as printed:
            prompt_args = ["prompt", "child", body]
            if producer_binding is not None:
                for value in (producer_binding["duty_id"], producer_binding["attempt_id"],
                              producer_binding["delivery_id"]):
                    prompt_args.extend(("--ref", value))
            args = peer_steward.build_parser().parse_args(prompt_args)
            rc = peer_steward.cmd_prompt(args, producer_binding=producer_binding,
                                        on_prepared=on_prepared)
            if state != "idle":
                self.assertEqual(screen_reader.call_count, 0)
        record = self._all_records()[-1]
        row = peer_steward.peer_message._read_pending(record["transfer_ref"])
        return rc, printed.call_args[0][0], row, sent, schedule

    def test_registered_runtime_prompt_preserves_producer_through_sender_duty_and_receipt(self):
        import dispatch_pending_delivery as pending
        import dispatch_session_sweep as sweep

        producer = {
            "schema": peer_steward.peer_message._RUNTIME_PRODUCER_SCHEMA,
            "duty_id": "registered-batch-" + "a" * 32,
            "jobs": str(Path(self._tmp.name) / "jobs.log"),
            "attempt_id": "att-runtime-completion",
            "carrier": "claude-parent-runtime",
            "delivery_id": "delivery-runtime-completion",
            "receipt_digest": "b" * 64,
            "recipient": {"harness": "claude", "session_id": "sid-child"},
            "route_id": "rt-runtime", "route_node": "execute",
        }
        jobs = Path(producer["jobs"])
        jobs.touch()
        sid, attempt, did = "sid-child", producer["attempt_id"], producer["delivery_id"]
        receipt = dict(schema_version=2, state="delivered", job_registry=str(jobs),
            parent_attempt_id="att-runtime-parent", children=[dict(attempt_id=attempt,
                status="done", readiness="ready", reason="terminal-failure-or-unclosed",
                required_action="inspect-done-failure")])
        stored = pending.create(jobs.parent, recipient_kind=producer["carrier"], recipient_key=sid,
            delivery_id=did, session_generation="", session_generation_supported="0",
            attempt_ids=[attempt], parent_attempt_id="att-runtime-parent",
            route_id=producer["route_id"], route_node=producer["route_node"], receipt=receipt,
            receipt_digest=pending._canonical_receipt_digest(receipt), row_revisions={attempt: "revision-runtime"})
        producer["receipt_digest"] = stored["receipt_digest"]
        _register_completion_fixture(jobs, attempt, dict(parent_sid=sid, parent_harness="claude",
            parent_completion_delivery=producer["carrier"], delivery_intent="1", delivery_id=did,
            delivery_receipt_digest=stored["receipt_digest"], delivery_row_revision="revision-runtime",
            route_id=producer["route_id"], route_node=producer["route_node"]))
        store = peer_steward.peer_obligations.ObligationStore(jobs.parent)
        store.create(producer["duty_id"], "registered-batch", dict(jobs=str(jobs), attempt_id=attempt,
            session_id=sid, harness="claude", server="default"), dict(carrier=producer["carrier"]))
        owner = "retained-rewake:" + producer["duty_id"]
        pending.claim(jobs.parent, sid, did, claim_owner=owner, lease_seconds=30,
                      require_generation_proof=False, jobs=jobs)
        binding = dict(duty_id=producer["duty_id"], jobs=str(jobs), attempt_id=attempt,
            carrier=producer["carrier"], recipient_sid=sid, storage_recipient=sid, delivery_id=did,
            receipt_digest=stored["receipt_digest"], claim_owner=owner)
        body = "[steer] exact private recipient ref\n" + sweep.delivery_context([
            (jobs.parent, [{**stored, sweep.STORAGE_KEY: sid}])])
        rc, line, row, sent, schedule = self._run_claude_prompt(
            consume=True, producer_binding=producer, body=body,
            on_prepared=lambda ref: peer_steward.peer_message.bind_dispatch_notice(ref, binding))
        self.assertEqual(rc, 0, line)
        self.assertIn("verify=exact-peer-ref", line)
        self.assertIn("runtime-from:", sent[0][1])
        self.assertEqual(row["state"], "received")
        self.assertEqual(row["from"], {"harness": "runtime", "session_id": None,
                                        "name": "Hearting runtime"})
        self.assertEqual(row["producer_binding"], producer)
        schedule.assert_not_called()
        rows = [item for item in self._all_records()
                if item.get("transfer_ref") == row["ref"]]
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(item["from"]["harness"] == "runtime"
                            and item.get("producer_binding") == producer for item in rows))
        duty = peer_steward._schedule_message_obligation(row, "child")
        self.assertEqual(duty["intent"]["from"], row["from"])
        self.assertEqual(duty["intent"]["producer_binding"], producer)

    def _run_sidless_prompt(self, guard_results, *, send_result=(0, {}), text="[steer] first Codex turn"):
        os.environ["CLAUDE_CODE_SESSION_ID"] = "sid-steward"
        proof = {"schema": "peer-first-input-process-v1", "harness": "codex",
                 "pane_id": "w1:pX", "pid": os.getpid(), "start": "123",
                 "pid_namespace": os.readlink("/proc/self/ns/pid")}
        self._native_sid_by_pid.pop(41000, None)
        sent = []
        guard = mock.Mock(side_effect=guard_results)
        with mock.patch.object(peer_steward.shutil, "which", return_value="/usr/bin/herdr"), \
             mock.patch.object(peer_steward, "_current_session_identity",
                               return_value=("sid-steward", "claude")), \
             mock.patch.object(peer_steward, "_from_name", return_value="steward"), \
             mock.patch.object(peer_steward, "_project_of", return_value="/fixture"), \
             mock.patch.object(peer_steward, "_resolve_target",
                               return_value=("codex", None, "fresh")), \
             mock.patch.object(peer_steward, "_prompt_agent_inventory",
                               return_value=([self._inventory_row(
                                   harness="codex", sid=None, name="fresh")], None)), \
             mock.patch.object(peer_steward, "_retire_target",
                               return_value=self._current_target(
                                   harness="codex", sid=None, name="fresh")), \
             mock.patch.object(peer_steward, "_retire_pane_info", side_effect=self._pane_info_for), \
             mock.patch.object(peer_steward, "_agent_state", return_value=("idle", "w1:pX")), \
             mock.patch.object(peer_steward, "_first_input_foreground_proof", return_value=proof), \
             mock.patch.object(peer_steward, "_sidless_idle_prompt_guard", guard), \
             mock.patch.object(peer_steward, "_herdr_prompt",
                               side_effect=lambda target, body, **kw:
                               (sent.append((target, body, kw)) or send_result)), \
             mock.patch.object(peer_steward, "_schedule_message_obligation") as schedule, \
             mock.patch("builtins.print") as printed:
            args = peer_steward.build_parser().parse_args(["prompt", "w1:pX", text])
            rc = peer_steward.cmd_prompt(args)
        record = self._all_records()[-1]
        row = peer_steward.peer_message._read_pending(record["transfer_ref"])
        return rc, printed.call_args[0][0], row, sent, schedule, guard.call_count

    def test_sidless_codex_first_input_submits_one_private_trailer_and_stays_queued(self):
        proof = {"schema": "peer-first-input-process-v1", "harness": "codex",
                 "pane_id": "w1:pX", "pid": os.getpid(), "start": "123",
                 "pid_namespace": os.readlink("/proc/self/ns/pid")}
        rc, line, row, sent, schedule, guard_calls = self._run_sidless_prompt(
            [(None, proof), (None, proof)])
        self.assertEqual(rc, 3, line)
        self.assertEqual((row["state"], row["receipt"]),
                         ("queued", "native-first-input-submitted"))
        self.assertEqual(row["first_input_process"]["pid"], os.getpid())
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][0], "w1:pX")
        self.assertIn("peer-from:", sent[0][1])
        self.assertEqual(guard_calls, 2)  # before claim, then after claim at submit boundary
        schedule.assert_not_called()

    def test_sidless_target_process_race_defers_without_submitting(self):
        proof = {"schema": "peer-first-input-process-v1", "harness": "codex",
                 "pane_id": "w1:pX", "pid": os.getpid(), "start": "123",
                 "pid_namespace": os.readlink("/proc/self/ns/pid")}
        rc, line, row, sent, schedule, _calls = self._run_sidless_prompt(
            [(None, proof), ("target-process-changed", None)])
        self.assertEqual(rc, 3, line)
        self.assertIn("reason=target-process-changed", line)
        self.assertEqual((row["state"], row.get("rpc_claim")), ("pending", None))
        self.assertEqual(sent, [])
        schedule.assert_called_once()

    def test_sidless_busy_draft_form_and_unknown_proof_all_defer_safely(self):
        proof = {"schema": "peer-first-input-process-v1", "harness": "codex",
                 "pane_id": "w1:pX", "pid": os.getpid(), "start": "123",
                 "pid_namespace": os.readlink("/proc/self/ns/pid")}
        for reason in ("native-state-working", "draft", "form-open", "foreground-unverified"):
            with self.subTest(reason=reason):
                rc, line, row, sent, schedule, calls = self._run_sidless_prompt([(reason, None)])
                self.assertEqual(rc, 3, line)
                self.assertIn("first-input-deferred", line)
                self.assertEqual(row["state"], "pending")
                self.assertEqual(sent, [])
                self.assertEqual(calls, 1)
                schedule.assert_called_once()

    def test_sidless_ambiguous_prompt_submission_is_never_retried(self):
        proof = {"schema": "peer-first-input-process-v1", "harness": "codex",
                 "pane_id": "w1:pX", "pid": os.getpid(), "start": "123",
                 "pid_namespace": os.readlink("/proc/self/ns/pid")}
        rc, line, row, sent, schedule, _calls = self._run_sidless_prompt(
            [(None, proof), (None, proof)], send_result=(None, None))
        self.assertEqual(rc, 5, line)
        self.assertEqual(row["state"], "unverified")
        self.assertEqual(row["receipt"], "native-first-input-inflight")
        self.assertIsNotNone(row.get("rpc_claim"))
        self.assertEqual(len(sent), 1)
        schedule.assert_called_once()

    def test_sidless_guard_requires_idle_empty_composer_exact_pane_and_same_process(self):
        process = {"schema": "peer-first-input-process-v1", "harness": "codex",
                   "pane_id": "w1:pX", "pid": os.getpid(), "start": "123",
                   "pid_namespace": os.readlink("/proc/self/ns/pid")}

        def guard(harness, status, screen, observed=process, expected=process):
            identity = {"harness": harness, "session_id": "-", "pane": "w1:pX"}
            with mock.patch.object(peer_steward, "_retire_target",
                                   return_value=(status, identity, 0, None)), \
                 mock.patch.object(peer_steward, "_agent_state", return_value=(status, "w1:pX")), \
                 mock.patch.object(peer_steward, "_pane_readiness",
                                   return_value=SimpleNamespace(state="ready", reason="")), \
                 mock.patch.object(peer_steward, "_first_input_foreground_proof",
                                   return_value=observed), \
                 mock.patch.object(peer_steward, "_read_screen",
                                   return_value=peer_steward._screen_lines(screen)):
                return peer_steward._sidless_idle_prompt_guard(
                    "fresh", harness, "w1:pX", expected_foreground=expected)[0]

        self.assertIsNone(guard("codex", "idle", CODEX_EMPTY))
        self.assertEqual(guard("codex", "working", CODEX_EMPTY), "native-state-working")
        self.assertEqual(guard("codex", "idle", CODEX_DRAFT), "draft")
        self.assertEqual(guard("codex", "idle", CODEX_POPUP), "draft-unknown")
        self.assertEqual(guard("claude", "idle", CLAUDE_FORM,
                               observed={**process, "harness": "claude"},
                               expected={**process, "harness": "claude"}), "form-open")
        self.assertEqual(guard("codex", "idle", CODEX_EMPTY,
                               observed={**process, "start": "replaced-start"}),
                         "target-process-changed")

    def test_idle_claude_empty_composer_uses_exact_prompt_and_receiver_ref(self):
        rc, line, row, sent, schedule = self._run_claude_prompt(consume=True)
        self.assertEqual(rc, 0, line)
        self.assertIn("verify=exact-peer-ref", line)
        self.assertEqual(row["state"], "received")
        self.assertEqual(row["receipt"], "exact-peer-ref")
        self.assertEqual(len(sent), 1)
        self.assertEqual((sent[0][0], sent[0][2]), ("w1:pX", False))
        self.assertEqual(schedule.call_count, 0)

    def test_idle_prompt_return_stays_queued_without_exact_receiver_ref(self):
        rc, line, row, sent, _schedule = self._run_claude_prompt()
        self.assertEqual(rc, 3, line)
        self.assertIn("verify=native-prompt-submitted", line)
        self.assertEqual((row["state"], row["receipt"]),
                         ("queued", "claude-pty-prompt-submitted"))
        self.assertEqual(len(sent), 1)

    def test_ambiguous_claude_prompt_reply_keeps_exact_claim_and_owner_without_retry(self):
        rc, line, row, sent, schedule = self._run_claude_prompt(
            send_result=(None, None))
        self.assertEqual(rc, 5, line)
        self.assertIn("verify=native-prompt-unverified", line)
        self.assertEqual(row["state"], "unverified")
        self.assertIsNotNone(row.get("rpc_claim"))
        self.assertEqual(len(sent), 1)
        schedule.assert_called_once()

    def test_busy_claude_uses_callback_without_reading_or_typing_into_pane(self):
        rc, line, row, sent, schedule = self._run_claude_prompt(
            state="working", screen="busy screen is intentionally unread")
        self.assertEqual(rc, 3, line)
        self.assertIn("verify=native-callback", line)
        self.assertEqual(row["state"], "pending")
        self.assertEqual(sent, [])
        schedule.assert_called_once()

    def test_busy_claude_with_native_busy_readiness_stays_callback_owned(self):
        readiness = SimpleNamespace(state="pending", reason="native-turn-busy")
        rc, line, row, sent, schedule = self._run_claude_prompt(
            state="working", readiness=readiness)
        self.assertEqual(rc, 3, line)
        self.assertIn("verify=native-callback", line)
        self.assertEqual((row["state"], sent), ("pending", []))
        schedule.assert_called_once()

    def test_prompt_foreground_proof_requires_sid_pid_birth_and_namespace(self):
        pid = 424242
        group = pid
        info = {"pane_id": "w1:pX", "shell_pid": 123,
                "foreground_process_group_id": group,
                "foreground_processes": [{"pid": pid, "argv": ["/usr/bin/claude", "--tui"]}]}
        fields = ["S", "1", str(group), "0", "0", *(["0"] * 14), "123"]
        stat = f"{pid} (claude) " + " ".join(fields)

        def prove(*, session_ids=("sid-child", "sid-child"),
                  starts=("123", "123"),
                  target_ns="pid:[77]", sender_ns="pid:[77]"):
            def read_text(path, *args, **kwargs):
                if str(path) == f"/proc/{pid}/stat":
                    return stat
                if str(path) == f"/proc/{pid}/comm":
                    return "claude\n"
                raise AssertionError(f"unexpected proc read: {path}")
            def readlink(path):
                return sender_ns if str(path) == "/proc/self/ns/pid" else target_ns
            with mock.patch.object(peer_steward, "_retire_pane_info", return_value=info), \
                 mock.patch.object(peer_steward, "_prompt_foreground", return_value={"pid": pid, "start": "123"}), \
                 patch_split(peer_steward, "_proc_start_ticks", side_effect=starts), \
                 mock.patch.object(peer_steward, "_prompt_process_session", side_effect=[(sid, None) for sid in session_ids]), \
                 mock.patch.object(peer_steward.os, "readlink", side_effect=readlink):
                return peer_steward._prompt_foreground_proof("w1:pX", "claude", "sid-child")

        proof = prove()
        self.assertEqual(proof, {"pid": pid, "start": "123",
                                 "pid_namespace": "pid:[77]", "session_id": "sid-child"})
        self.assertIsNone(prove(session_ids=("sid-child", "different-sid")))
        self.assertIsNone(prove(starts=("123", "456")))
        self.assertIsNone(prove(target_ns="pid:[other]"))

    def test_forms_drafts_unknown_boxes_and_blocked_state_never_receive_prompt_input(self):
        for state, screen in (("idle", CLAUDE_FORM), ("idle", CLAUDE_DRAFT),
                              ("idle", "screen without a composer"),
                              ("blocked", CLAUDE_EMPTY)):
            with self.subTest(state=state, screen=screen[:20]):
                rc, line, row, sent, _schedule = self._run_claude_prompt(state=state, screen=screen)
                self.assertEqual(rc, 3, line)
                self.assertEqual(row["state"], "pending")
                self.assertEqual(sent, [])

    def test_foreground_birth_change_between_checks_releases_unsent_claim(self):
        first = {"pid": os.getpid(), "start": "old-start",
                 "pid_namespace": os.readlink("/proc/self/ns/pid"), "session_id": "sid-child"}
        changed = {**first, "start": "new-start"}
        rc, line, row, sent, schedule = self._run_claude_prompt(
            foregrounds=[first, changed])
        self.assertEqual(rc, 3, line)
        self.assertIn("target-process-changed", line)
        self.assertEqual(row["state"], "pending")
        self.assertIsNone(row.get("rpc_claim"))
        self.assertEqual(sent, [])
        schedule.assert_called_once()

    def test_codex_prompt_uses_exact_native_queue_and_never_prompt_typing(self):
        os.environ["CLAUDE_CODE_SESSION_ID"] = "sid-steward"
        calls, queued = [], []
        payload = _agent_json("codex", "exact-thread", "child", status="working")

        def accept(ref, *, timeout=1.0, **_kw):
            row = peer_steward.peer_message._read_pending(ref)
            queued.append((row["to"]["session_id"], ref, row["text"]))
            return {"status": "queued", "reason": "native-queue-accepted"}

        with mock.patch.object(peer_steward.shutil, "which", return_value="/usr/bin/herdr"), \
             mock.patch.object(peer_steward.subprocess, "run", side_effect=self._fake_run(payload, calls)), \
             mock.patch.object(peer_steward.peer_message, "deliver_pending_codex", side_effect=accept), \
             mock.patch("builtins.print") as printed:
            rc = peer_steward.main(["prompt", "child", "[handoff] continue"])
        self.assertEqual(rc, 3)
        self.assertEqual(len(queued), 1)
        self.assertEqual(queued[0][0], "exact-thread")
        self.assertEqual(self._pane_io_calls(calls), [])
        self.assertIn("native-queue-accepted", printed.call_args[0][0])
        record = self._all_records()[-1]
        self.assertEqual(record["delivery"]["status"], "unknown")
        self.assertIn("verify=native-queue-queued", record["delivery"]["receipt"])

    # --- D1 end-to-end: real prompt -> real deliver_pending_codex -> real send_at_least_once ---
    class _AppServer:
        """Method-keyed fake Codex app-server; records every RPC."""

        def __init__(self, *, turn_status="completed", queue=(), turn_id="turn-live"):
            self.turn_status, self.turn_id = turn_status, turn_id
            self.queue = [dict(item) for item in queue]
            self.calls, self.errors, self.history_state = [], {}, "not_observed"
            self.added = 0

        def methods(self):
            return [method for method, _params in self.calls]

        def params(self, method):
            return [params for name, params in self.calls if name == method]

        def __call__(self, _path, method, params, **_kw):
            self.calls.append((method, dict(params)))
            error = self.errors.get(method)
            if error is not None:
                raise error
            if method == "thread/queue/list":
                return {"data": [dict(item) for item in self.queue]}
            if method == "thread/turns/list":
                if self.turn_status is None:
                    return {"data": []}
                return {"data": [{"id": self.turn_id, "status": self.turn_status, "items": []}]}
            if method == "thread/queue/add":
                self.added += 1
                item = {"id": "queued-%d" % self.added, "input": params["input"],
                        "clientUserMessageId": params["clientUserMessageId"]}
                self.queue.append(item)
                return {"queuedSubmission": {"id": item["id"]}}
            if method == "thread/queue/delete":
                self.queue = [i for i in self.queue if i["id"] != params["queuedSubmissionId"]]
                return {"deleted": True}
            if method == "thread/queue/start":
                return {"turn": {"id": "turn-started"}}
            if method == "turn/steer":
                return {"turnId": params["expectedTurnId"]}
            raise AssertionError("unexpected rpc " + method)

    @contextlib.contextmanager
    def _codex_e2e(self, server, *, screen=None):
        """Patch only the transport: RPC, native history observation, and the visible screen."""
        import codex_queue_delivery
        self._readiness_patch.stop()
        old_session = peer_steward._HERDR_SESSION
        peer_steward._HERDR_SESSION = "selected"
        self.addCleanup(setattr, peer_steward, "_HERDR_SESSION", old_session)
        sid = "codex-thread-e2e"
        payload = _agent_json("codex", sid, "child", status="idle")
        screens = {"value": screen if screen is not None else CODEX_EMPTY}
        state = SimpleNamespace(server=server, sid=sid, screens=screens, output=[])

        def observe(_path, _thread, _client_id, *, timeout, expected_message=None):
            return codex_queue_delivery._PeerObservation(state.server.history_state)

        def run_prompt(body, *, sender=("sender-session", "claude")):
            state.output.clear()
            with contextlib.ExitStack() as stack:
                stack.enter_context(mock.patch.object(peer_steward.subprocess, "run",
                                                       side_effect=self._fake_run(payload, [])))
                stack.enter_context(mock.patch.object(
                    peer_steward, "_retire_target",
                    side_effect=lambda pane: ("idle", {"harness": "codex", "session_id": sid,
                                                       "name": "child", "pane": pane}, 0, None)))
                stack.enter_context(mock.patch.object(
                    peer_steward, "_retire_pane_info",
                    side_effect=lambda _pane: self._codex_process_info([41002])))
                stack.enter_context(mock.patch.object(
                    peer_steward, "_prompt_foreground",
                    side_effect=lambda _pane, harness, *, strict=True, info=None:
                    {"pid": 41002, "start": "birth", "argv": [harness]}))
                stack.enter_context(mock.patch.object(peer_steward, "_current_session_identity",
                                                       return_value=sender))
                stack.enter_context(mock.patch.object(peer_steward, "_from_name", return_value="sender"))
                stack.enter_context(mock.patch.object(peer_steward.peer_obligations,
                                                       "bound_work_for_pane", return_value=((), "observed")))
                import pane_ownership
                stack.enter_context(mock.patch.object(pane_ownership, "current_codex_thread",
                                                       side_effect=lambda *_a, **_k: sid))
                stack.enter_context(mock.patch.object(
                    peer_steward, "_read_screen",
                    side_effect=lambda _target: peer_steward._screen_lines(state.screens["value"])))
                printed = stack.enter_context(mock.patch("builtins.print"))
                rc = peer_steward.main(["prompt", "child", body])
                state.output = [call.args[0] for call in printed.call_args_list if call.args]
            ref = self._all_records()[-1]["transfer_ref"]
            return rc, "\n".join(state.output), ref

        state.run = run_prompt
        with mock.patch("codex_queue_dispatch.resolve_queue_socket", return_value="fixture-socket"), \
             mock.patch.object(codex_queue_delivery, "_rpc", server), \
             mock.patch.object(codex_queue_delivery, "_observe_peer_message", side_effect=observe):
            yield state

    def test_d1_interrupted_empty_composer_starts_exact_hearting_item(self):
        server = self._AppServer(turn_status="interrupted")
        with self._codex_e2e(server) as env:
            rc, line, ref = env.run("[steer] resume the work")
        self.assertEqual(rc, 3, line)
        self.assertIn("reason=native-queue-started", line)
        starts = server.params("thread/queue/start")
        self.assertEqual(starts, [{"threadId": env.sid, "queuedSubmissionId": "queued-1"}])
        self.assertEqual(server.params("thread/queue/add")[0]["clientUserMessageId"], "delivery-peer-" + ref)
        row = peer_steward.peer_message._read_pending(ref)
        self.assertEqual((row["state"], row["receipt"]), ("queued", "native-queue-started"))

    def test_d1_interrupted_hold_without_start(self):
        form = "banner: project · main\nPrior chat text\n" + _codex_snapshot("worktrees_new_choices.snap")
        cases = (("draft", CODEX_DRAFT, ()), ("form", form, ()),
                 ("user-queue", CODEX_EMPTY, ({"id": "user-1", "input": [{"type": "text", "text": "mine"}]},)))
        for expected, screen, queue in cases:
            with self.subTest(hold=expected):
                server = self._AppServer(turn_status="interrupted", queue=queue)
                with self._codex_e2e(server, screen=screen) as env:
                    rc, line, ref = env.run("[steer] resume the work " + expected)
                self.assertEqual(rc, 3, line)
                self.assertEqual(server.params("thread/queue/start"), [])
                self.assertIn("reason=native-queue-held-interrupted:" + expected, line)
                self.assertEqual(server.methods().count("thread/queue/add"), 1)
                self.assertTrue(all(item["id"] != "user-1" for item in server.params("thread/queue/delete")))

    def test_d2_in_progress_steers_active_turn(self):
        server = self._AppServer(turn_status="inProgress")
        with self._codex_e2e(server) as env:
            rc, line, ref = env.run("plain adjustment for the running turn")
        self.assertEqual(rc, 3, line)
        self.assertIn("verify=native-steer-queued", line)
        self.assertIn("reason=native-steer-accepted", line)
        text = peer_steward.peer_message._read_pending(ref)["text"]
        self.assertEqual(server.params("turn/steer"), [{
            "threadId": env.sid, "expectedTurnId": "turn-live",
            "clientUserMessageId": "delivery-peer-" + ref,
            "input": [{"type": "text", "text": text}]}])
        self.assertEqual(server.params("thread/queue/add"), [])
        row = peer_steward.peer_message._read_pending(ref)
        self.assertEqual((row["state"], row["receipt"], row["steer_turn_id"]),
                         ("queued", "native-steer-accepted", "turn-live"))

    def test_d2_steer_refusal_falls_back_to_queue_add(self):
        server = self._AppServer(turn_status="inProgress")
        server.errors["turn/steer"] = _queue_error(
            "queue-rpc-refused:turn/steer:-32600:expected turn mismatch", ambiguous=False)
        with self._codex_e2e(server) as env:
            rc, line, ref = env.run("plain adjustment, steer will be refused")
        self.assertEqual(rc, 3, line)
        self.assertIn("reason=native-queue-behind-active-turn", line)
        self.assertEqual(server.methods().count("thread/queue/add"), 1)

    def test_d2_ambiguous_steer_does_not_add(self):
        server = self._AppServer(turn_status="inProgress")
        server.errors["turn/steer"] = _queue_error("queue-websocket-timeout", ambiguous=True)
        with self._codex_e2e(server) as env:
            rc, line, ref = env.run("plain adjustment, steer reply is lost")
        self.assertEqual(rc, 5, line)
        self.assertIn("prompted=unverified", line)
        self.assertEqual(server.params("thread/queue/add"), [])
        row = peer_steward.peer_message._read_pending(ref)
        self.assertEqual((row["state"], row["receipt"], row["steer_turn_id"]),
                         ("unverified", "native-steer-inflight", "turn-live"))

    def test_d2_lost_steer_is_requeued_exactly_once(self):
        server = self._AppServer(turn_status="inProgress")
        with self._codex_e2e(server) as env:
            rc, line, ref = env.run("plain adjustment that Codex later drops")
            self.assertIn("native-steer-accepted", line)
            server.turn_status = "completed"
            first = peer_steward.peer_message.deliver_pending_codex(ref)
            second = peer_steward.peer_message.deliver_pending_codex(ref)
        self.assertEqual(first["status"], "queued", first)
        self.assertEqual(second["status"], "queued", second)
        self.assertEqual(server.methods().count("thread/queue/add"), 1)
        self.assertEqual(server.methods().count("turn/steer"), 1)
        self.assertEqual(peer_steward.peer_message._read_pending(ref)["receipt"], "native-queue-accepted")

    def test_d3_prior_same_sender_rows_are_promoted_before_new_steer(self):
        server = self._AppServer(turn_status="completed",
                                 queue=({"id": "user-1", "input": [{"type": "text", "text": "mine"}]},))
        pm = peer_steward.peer_message
        with self._codex_e2e(server) as env:
            _rc, _line, first_ref = env.run("older note one")
            _rc, _line, second_ref = env.run("older note two")
            foreign_sender = {"harness": "claude", "session_id": "other-sender", "name": "other"}
            recipient = {"harness": "codex", "session_id": env.sid, "name": "child", "pane_id": "w1:pX"}
            _text, foreign_ref = pm.prepare_peer_message(
                "foreign note", foreign_sender, recipient, defer=True)
            server.queue.append({"id": "foreign-1", "clientUserMessageId": "delivery-peer-" + foreign_ref,
                                 "input": [{"type": "text", "text": _text}]})
            with pm.pending_lock(foreign_ref):
                pm._save_pending(dict(pm._read_pending(foreign_ref), state="queued",
                                      receipt="native-queue-accepted"))
            server.calls.clear()
            server.turn_status = "inProgress"
            rc, line, new_ref = env.run("newest note while running")
        self.assertEqual(rc, 3, line)
        steers = [params["clientUserMessageId"] for params in server.params("turn/steer")]
        self.assertEqual(steers, ["delivery-peer-" + first_ref, "delivery-peer-" + second_ref,
                                  "delivery-peer-" + new_ref])
        deleted = [params["queuedSubmissionId"] for params in server.params("thread/queue/delete")]
        self.assertEqual(deleted, ["queued-1", "queued-2"])
        self.assertEqual([m for m in server.methods() if m in {"thread/queue/delete", "turn/steer"}],
                         ["thread/queue/delete", "turn/steer", "thread/queue/delete", "turn/steer", "turn/steer"])
        self.assertEqual(server.params("thread/queue/add"), [])
        self.assertEqual({item["id"] for item in server.queue}, {"user-1", "foreign-1"})
        for ref in (first_ref, second_ref, new_ref):
            self.assertEqual(pm._read_pending(ref)["receipt"], "native-steer-accepted")
        self.assertEqual(pm._read_pending(foreign_ref)["receipt"], "native-queue-accepted")

    def _codex_prompt_receipt(self, result, screen):
        os.environ["CLAUDE_CODE_SESSION_ID"] = "sid-steward"
        calls, gates = [], []
        payload = _agent_json("codex", "exact-thread", "child", status="working")

        def deliver(ref, **kwargs):
            gates.append(kwargs.get("start_interrupted", "missing"))
            return result

        lines = None if screen is None else peer_steward._screen_lines(screen)
        with mock.patch.object(peer_steward.shutil, "which", return_value="/usr/bin/herdr"), \
             mock.patch.object(peer_steward.subprocess, "run", side_effect=self._fake_run(payload, calls)), \
             mock.patch.object(peer_steward, "_read_screen", return_value=lines), \
             mock.patch.object(peer_steward.peer_message, "deliver_pending_codex", side_effect=deliver), \
             mock.patch("builtins.print") as printed:
            rc = peer_steward.main(["prompt", "child", "[steer] receipt report"])
        return rc, printed.call_args[0][0], gates, self._all_records()[-1]["delivery"]["receipt"]

    def test_codex_prompt_passes_the_screen_gate_and_reports_the_real_receipt(self):
        cases = (
            (CODEX_EMPTY, True, {"status": "queued", "reason": "native-queue-started"},
             3, "verify=native-queue-queued", "reason=native-queue-started"),
            (CODEX_DRAFT, "draft", {"status": "queued", "reason": "native-queue-held-interrupted:draft"},
             3, "verify=native-queue-queued", "reason=native-queue-held-interrupted:draft"),
            (None, "screen-unknown", {"status": "queued", "reason": "native-queue-held-interrupted:screen-unknown"},
             3, "verify=native-queue-queued", "reason=native-queue-held-interrupted:screen-unknown"),
            (CODEX_EMPTY, True, {"status": "queued", "reason": "native-steer-accepted"},
             3, "verify=native-steer-queued", "reason=native-steer-accepted"),
            (CODEX_EMPTY, True, {"status": "unverified", "reason": "queue-websocket-timeout"},
             5, "verify=native-queue-unverified", "reason=queue-websocket-timeout"),
            (CODEX_EMPTY, True, {"status": "received", "reason": "exact-native-history"},
             0, "verify=native-queue-received", "reason=exact-native-history"),
        )
        for screen, gate, result, rc_expected, verify, reason in cases:
            with self.subTest(reason=result["reason"]):
                rc, line, gates, receipt = self._codex_prompt_receipt(result, screen)
                self.assertEqual((rc, gates), (rc_expected, [gate]), line)
                self.assertIn(verify, line)
                self.assertIn(reason, line)
                self.assertIn(verify, receipt)

    def test_immediate_codex_restart_queues_the_original_sealed_thread_and_ref(self):
        rc, proofs, submissions, ref, pending = self._run_immediate_codex_restart("same-thread")
        self.assertEqual(rc, 3)
        self.assertEqual({proof for proof in proofs if proof[0] in {41001, 41002}}, {
            (41001, "birth-before-restart"), (41002, "birth-after-restart")})
        self.assertEqual(len(submissions), 1)
        self.assertEqual(submissions[0]["thread_id"], "codex-thread-original")
        self.assertEqual(submissions[0]["client_message_id"], "delivery-peer-" + ref)
        self.assertEqual(submissions[0]["message"], pending["text"])
        self.assertEqual((pending["ref"], pending["state"], pending["to"]["session_id"],
                          pending["to"]["pane_id"]),
                         (ref, "queued", "codex-thread-original", "w1:pX"))

    def test_immediate_codex_restart_still_rejects_changed_or_unknown_current_proof(self):
        for scenario in ("changed-sid", "changed-pane", "conflicting-proof", "unknown-proof"):
            with self.subTest(scenario=scenario):
                _rc, _proofs, submissions, _ref, _pending = \
                    self._run_immediate_codex_restart(scenario)
                self.assertEqual(submissions, [])

    def test_deferred_codex_restart_queues_the_original_sealed_thread_and_ref(self):
        store, duty, proofs, submissions, ref, pending = \
            self._run_deferred_codex_restart("same-thread")
        self.assertEqual({proof for proof in proofs}, {
            (42001, "birth-before-restart"), (42002, "birth-after-restart")})
        self.assertEqual(len(submissions), 1)
        self.assertEqual(submissions[0]["thread_id"], "codex-thread-original")
        self.assertEqual(submissions[0]["client_message_id"], "delivery-peer-" + ref)
        self.assertEqual(submissions[0]["message"], pending["text"])
        self.assertEqual((pending["ref"], pending["state"], pending["to"]["session_id"],
                          pending["to"]["pane_id"]),
                         (ref, "queued", "codex-thread-original", "w1:pDeferred"))
        self.assertEqual(store.get(duty["id"])["state"], "pending")

    def test_deferred_codex_restart_still_rejects_changed_or_unknown_current_proof(self):
        for scenario in ("changed-sid", "changed-pane", "conflicting-proof", "unknown-proof"):
            with self.subTest(scenario=scenario):
                store, duty, _proofs, submissions, _ref, pending = \
                    self._run_deferred_codex_restart(scenario)
                self.assertEqual(submissions, [])
                self.assertEqual(pending["state"], "pending")
                self.assertEqual(store.get(duty["id"])["state"], "unknown")

    def test_codex_exact_history_receipt_is_the_only_prompted_true(self):
        os.environ["CLAUDE_CODE_SESSION_ID"] = "sid-steward"
        payload = _agent_json("codex", "exact-thread", "child", status="idle")
        calls = []

        def consume(ref, *, timeout=1.0, **_kw):
            with peer_steward.peer_message.pending_lock(ref):
                row = peer_steward.peer_message._read_pending(ref)
                peer_steward.peer_message._save_pending(dict(
                    row, state="received", text=None, receipt="exact-native-history"))
            return {"status": "received", "reason": "exact-native-history"}

        with mock.patch.object(peer_steward.shutil, "which", return_value="/usr/bin/herdr"), \
             mock.patch.object(peer_steward.subprocess, "run", side_effect=self._fake_run(payload, calls)), \
             mock.patch.object(peer_steward.peer_message, "deliver_pending_codex", side_effect=consume), \
             mock.patch("builtins.print") as printed:
            rc = peer_steward.main(["prompt", "child", "[steer] exact receipt"])
        self.assertEqual(rc, 0)
        self.assertIn("prompted=true", printed.call_args[0][0])
        self.assertIn("verify=native-queue-received", printed.call_args[0][0])
        self.assertEqual(peer_steward.peer_message._read_pending(self._all_records()[-1]["transfer_ref"])["state"],
                         "received")
        self.assertEqual(self._pane_io_calls(calls), [])

    def test_sidless_target_remains_pane_bound_until_native_session_exists(self):
        os.environ["CLAUDE_CODE_SESSION_ID"] = "sid-steward"
        calls = []
        payload = _agent_json("codex", None, "fresh", status="idle")
        with mock.patch.object(peer_steward.shutil, "which", return_value="/usr/bin/herdr"), \
             mock.patch.object(peer_steward.subprocess, "run", side_effect=self._fake_run(payload, calls)), \
             mock.patch.object(peer_steward, "_proven_session", return_value=None), \
             mock.patch.object(peer_steward, "_process_session", return_value=None), \
             mock.patch("builtins.print") as printed:
            rc = peer_steward.main(["prompt", "fresh", "wait for the exact thread"])
        self.assertEqual(rc, 5)
        self.assertIn("reason=target-identity-unverified", printed.call_args[0][0])
        failed = self._all_records()[-1]
        self.assertNotIn("transfer_ref", failed)
        self.assertEqual(failed["delivery"]["status"], "unknown")
        self.assertEqual(len(list(peer_steward.peer_message._pending_rows())), 1)

        with mock.patch.object(peer_steward.shutil, "which", return_value="/usr/bin/herdr"), \
             mock.patch.object(peer_steward.subprocess, "run", side_effect=self._fake_run(payload, calls)), \
             mock.patch.object(peer_steward, "_proven_session", return_value=None), \
             mock.patch.object(peer_steward, "_process_session", return_value=None), \
             mock.patch("builtins.print") as printed:
            rc = peer_steward.main(["prompt", "w1:pX", "wait for the exact thread"])
        self.assertEqual(rc, 3)
        self.assertIn("reason=target-process-proof-unknown", printed.call_args[0][0])
        row = peer_steward.peer_message._read_pending(self._all_records()[-1]["transfer_ref"])
        self.assertEqual((row["state"], row["to"]["session_id"], row["to"]["pane_id"]),
                         ("pending", None, "w1:pX"))
        self.assertEqual(self._pane_io_calls(calls), [])

    def test_sidless_codex_target_uses_the_thread_its_pane_tui_proves(self):
        os.environ["CLAUDE_CODE_SESSION_ID"] = "sid-steward"
        calls, queued = [], []
        payload = _agent_json("codex", None, "fresh", status="idle")
        self._native_sid_by_pid[os.getpid()] = "proven-thread"

        def accept(ref, *, timeout=1.0, **_kw):
            queued.append(peer_steward.peer_message._read_pending(ref)["to"]["session_id"])
            return {"status": "queued", "reason": "native-queue-accepted"}

        with mock.patch.object(peer_steward.shutil, "which", return_value="/usr/bin/herdr"), \
             mock.patch.object(peer_steward.subprocess, "run", side_effect=self._fake_run(payload, calls)), \
             mock.patch.object(peer_steward.peer_message, "deliver_pending_codex", side_effect=accept), \
             mock.patch("builtins.print") as printed:
            rc = peer_steward.main(["prompt", "fresh", "[steer] reach the proven thread"])
        self.assertEqual(rc, 3)
        self.assertEqual(queued, ["proven-thread"])
        self.assertIn("verify=native-queue-queued", printed.call_args[0][0])
        self.assertNotIn("target-session-unresolved", printed.call_args[0][0])
        row = peer_steward.peer_message._read_pending(self._all_records()[-1]["transfer_ref"])
        self.assertEqual((row["state"], row["to"]["session_id"]), ("pending", "proven-thread"))
        self.assertEqual(self._pane_io_calls(calls), [])

    def test_a_refused_queue_rpc_that_left_the_row_for_the_carrier_is_reported_queued(self):
        os.environ["CLAUDE_CODE_SESSION_ID"] = "sid-steward"
        calls = []
        payload = _agent_json("codex", None, "fresh", status="idle")
        self._native_sid_by_pid[os.getpid()] = "proven-thread"
        refused = {"status": "unverified", "reason":
                   "queue-rpc-refused:thread/turns/list:-32600:not materialized yet"}
        with mock.patch.object(peer_steward.shutil, "which", return_value="/usr/bin/herdr"), \
             mock.patch.object(peer_steward.subprocess, "run", side_effect=self._fake_run(payload, calls)), \
             mock.patch.object(peer_steward.peer_message, "deliver_pending_codex", return_value=refused), \
             mock.patch.object(peer_steward, "_codex_first_prompt", return_value=None), \
             mock.patch("builtins.print") as printed:
            peer_steward.main(["prompt", "fresh", "[steer] first message not sent yet"])
        line = printed.call_args[0][0]
        self.assertIn("prompted=queued", line)
        self.assertIn("queue-rpc-refused:thread/turns/list", line)
        self.assertNotIn("prompted=unverified", line)

    def _first_prompt_row(self, text="first message"):
        sender = {"harness": "claude", "session_id": "sid-steward", "name": "s"}
        recipient = {"harness": "codex", "session_id": "thread-new", "name": None,
                     "pane_id": "w1:pX"}
        _body, ref = peer_steward.peer_message.prepare_peer_message(
            text, sender, recipient, defer=True, with_trailer=False, receipt="native-pending")
        return ref

    def _first_prompt_run(self, ref, holds, prompt=(0, {"result": {"type": "prompt"}})):
        """`holds(enters)` is the composer state after that many Enters (None unreadable)."""
        sent = []
        guard = mock.patch.object(peer_steward, "_native_idle_prompt_guard",
                                  return_value=(None, {}))
        send = mock.patch.object(peer_steward, "_pane_send",
                                 side_effect=lambda pane, op, value: sent.append((op, value)) or True)
        typed = mock.patch.object(peer_steward, "_herdr_prompt", return_value=prompt)
        with guard, send, typed as prompted, mock.patch.object(peer_steward.time, "sleep"), \
             mock.patch.object(peer_steward, "_codex_rollout_exists", return_value=False), \
             mock.patch.object(peer_steward, "_codex_composer_holds_text",
                               side_effect=lambda _pane: holds(len(sent))), \
             mock.patch.object(peer_steward, "_CODEX_FIRST_PROMPT_POLL_SEC", 0.02), \
             mock.patch.object(peer_steward, "_CODEX_ENTER_GAP_SEC", 0):
            result = peer_steward._codex_first_prompt(ref, "w1:pX", "thread-new")
        return result, sent, prompted

    def test_unmaterialized_codex_thread_is_prompted_once_and_enter_repeats_while_text_stays(self):
        ref = self._first_prompt_row()
        # The host prompt left the text in the composer; the second Enter submits.
        result, sent, prompted = self._first_prompt_run(ref, lambda enters: enters < 2)
        self.assertTrue(result)
        self.assertEqual(sent, [("send-keys", "Enter")] * 2)
        prompted.assert_called_once()
        self.assertEqual(prompted.call_args[0][:2], ("w1:pX", "first message"))
        row = peer_steward.peer_message._read_pending(ref)
        self.assertEqual((row["state"], row["receipt"], row["rpc_claim"]),
                         ("queued", "native-first-input-submitted", None))

    def test_unmaterialized_thread_sends_no_enter_when_the_prompt_submits(self):
        ref = self._first_prompt_row()
        result, sent, _prompted = self._first_prompt_run(ref, lambda _enters: False)
        self.assertTrue(result)
        self.assertEqual(sent, [])

    def test_unreadable_composer_never_gets_a_blind_enter(self):
        ref = self._first_prompt_row()
        result, sent, _prompted = self._first_prompt_run(ref, lambda _enters: None)
        self.assertIsNone(result)
        self.assertEqual(sent, [])
        self.assertEqual(peer_steward.peer_message._read_pending(ref)["state"], "unverified")

    def test_unsubmitted_first_prompt_leaves_the_row_for_the_carrier(self):
        ref = self._first_prompt_row()
        result, sent, _prompted = self._first_prompt_run(ref, lambda _enters: True)
        self.assertIsNone(result)
        self.assertEqual(len(sent), peer_steward._CODEX_FIRST_ENTER_RETRIES)
        self.assertEqual(peer_steward.peer_message._read_pending(ref)["state"], "unverified")

    def test_a_refused_host_prompt_presses_no_enter(self):
        ref = self._first_prompt_row()
        result, sent, _prompted = self._first_prompt_run(
            ref, lambda _enters: True, prompt=(1, {"error": {"code": "x"}}))
        self.assertIsNone(result)
        self.assertEqual(sent, [])

    def test_materialized_thread_keeps_the_queue_path_and_never_types(self):
        ref = self._first_prompt_row()
        done = {"status": "queued", "reason": "native-queue-accepted"}
        with mock.patch.object(peer_steward.peer_message, "deliver_pending_codex",
                               return_value=done), \
             mock.patch.object(peer_steward, "_pane_send") as send:
            result = peer_steward._deliver_codex_pending(
                ref, "w1:pX", timeout=1.0, start_interrupted=False)
        self.assertEqual(result, done)
        send.assert_not_called()

    def test_a_submitted_first_prompt_is_not_delivered_again(self):
        ref = self._first_prompt_row()
        refused = {"status": "unverified", "reason":
                   "queue-rpc-refused:thread/turns/list:-32600:thread is not materialized yet"}
        with mock.patch.object(peer_steward.peer_message, "deliver_pending_codex",
                               return_value=refused) as queue, \
             mock.patch.object(peer_steward, "_codex_first_prompt", return_value=True) as first:
            result = peer_steward._deliver_codex_pending(
                ref, "w1:pX", timeout=1.0, start_interrupted=False)
        self.assertEqual(result["reason"], "native-first-input-submitted")
        first.assert_called_once_with(ref, "w1:pX", "thread-new")
        self.assertEqual(queue.call_count, 1)
        # A row that is no longer pending (already submitted) is never typed again.
        peer_steward.peer_message._save_pending(
            dict(peer_steward.peer_message._read_pending(ref), state="queued"))
        with mock.patch.object(peer_steward, "_pane_send") as send, \
             mock.patch.object(peer_steward, "_native_idle_prompt_guard", return_value=(None, {})):
            self.assertIsNone(peer_steward._codex_first_prompt(ref, "w1:pX", "thread-new"))
        send.assert_not_called()

    def test_same_sender_recipient_body_retry_reuses_the_same_ref(self):
        os.environ["CLAUDE_CODE_SESSION_ID"] = "sid-steward"
        payload = _agent_json("claude", "sid-child", "child", status="idle")
        refs = []
        with mock.patch.object(peer_steward.shutil, "which", return_value="/usr/bin/herdr"), \
             mock.patch.object(peer_steward.subprocess, "run", side_effect=self._fake_run(payload)), \
             mock.patch("builtins.print"):
            for _ in range(2):
                self.assertEqual(peer_steward.main(["prompt", "child", "retry this exact body"]), 3)
                refs.append(self._all_records()[-1]["transfer_ref"])
        self.assertEqual(refs[0], refs[1])
        self.assertEqual(peer_steward.peer_message._read_pending(refs[0])["state"], "pending")

    def test_whole_prompt_resolves_name_tag_and_pane_for_each_harness(self):
        self._readiness_patch.stop()
        peer_steward._HERDR_SESSION = "selected"
        for harness in ("claude", "codex", "opencode"):
            with self.subTest(harness=harness):
                sid = "sid-" + harness
                payload = _agent_json(harness, sid, "child", status="idle")
                calls = []
                self._codex_server_observations.clear()
                proof = {"pid": os.getpid(), "start": "fixture-start",
                         "pid_namespace": os.readlink("/proc/self/ns/pid"),
                         "session_id": sid}
                callback_sid = {"value": sid}

                def foreground(pane, kind, *, strict=True, info=None):
                    return {"pid": os.getpid(), "start": "fixture-start",
                            "argv": [kind], "shell_pid": os.getpid(),
                            "shell_start": peer_steward._proc_start_ticks(os.getpid())}

                def submit(pane, text, *, wait, timeout_ms, start_aware=False):
                    self.assertEqual(pane, "w1:pX")
                    self.assertFalse(wait)
                    received = peer_steward.peer_message.receive_peer_message(
                        text, {"harness": harness, "session_id": callback_sid["value"]})
                    self.assertEqual(received, 0)
                    return 0, {}

                def queue(ref, *, timeout=1.0, **_kw):
                    with peer_steward.peer_message.pending_lock(ref):
                        row = peer_steward.peer_message._read_pending(ref)
                        peer_steward.peer_message._save_pending(dict(
                            row, state="queued", receipt="native-queue-accepted"))
                    return {"status": "queued", "reason": "native-queue-accepted"}

                with mock.patch.object(peer_steward.subprocess, "run",
                                       side_effect=self._fake_run(payload, calls)), \
                     mock.patch.object(peer_steward, "_current_session_identity",
                                       return_value=("sender-session", "claude")), \
                     mock.patch.object(peer_steward, "_from_name",
                                       side_effect=lambda h, s: "sender" if s == "sender-session" else None), \
                     mock.patch.object(peer_steward, "_prompt_foreground",
                                       side_effect=foreground), \
                     mock.patch.object(peer_steward, "_prompt_foreground_proof",
                                       return_value=proof), \
                     mock.patch.object(peer_steward, "_read_screen",
                                       side_effect=lambda _target: peer_steward._screen_lines(
                                           CLAUDE_EMPTY if harness == "claude"
                                           else OPENCODE_FORM if harness == "opencode"
                                           else CODEX_EMPTY)), \
                     mock.patch.object(peer_steward.herdr_observation, "read_native_status",
                                       return_value={"status": "idle"}) as native_status, \
                     mock.patch("fleet.session_handle.resolve_recipient_tag",
                                return_value="3e") as tag_lookup, \
                     mock.patch.object(peer_steward, "_herdr_prompt", side_effect=submit) as send, \
                     mock.patch.object(peer_steward.peer_message, "deliver_pending_codex",
                                       side_effect=queue) as codex_queue, \
                     mock.patch("builtins.print") as printed:
                    for address in ("child", "[3e]", "w1:pX"):
                        rc = peer_steward.main([
                            "prompt", address, f"[steer] {harness} via {address}"])
                        self.assertEqual(rc, 0 if harness == "claude" else 3,
                                         printed.call_args[0][0])
                    if harness == "claude":
                        callback_sid["value"] = "wrong-current-session"
                        rc = peer_steward.main([
                            "prompt", "child", "[steer] mismatched callback cannot receive"])
                        self.assertEqual(rc, 3, printed.call_args[0][0])
                        self.assertIn("prompted=queued", printed.call_args[0][0])

                records = [row for row in self._all_records()
                           if row.get("transfer_ref")
                           and row.get("kind") == "steer"
                           and row.get("from", {}).get("session_id") == "sender-session"
                           and row.get("to", {}).get("harness") == harness]
                self.assertEqual(len(records), 4 if harness == "claude" else 3)
                for address, ledger in zip(("child", "[3e]", "w1:pX"), records):
                    pending = peer_steward.peer_message._read_pending(ledger["transfer_ref"])
                    self.assertEqual((pending["to"]["harness"], pending["to"]["session_id"],
                                      pending["to"]["pane_id"]), (harness, sid, "w1:pX"))
                    self.assertEqual((ledger["to"]["harness"], ledger["to"]["session_id"],
                                      ledger["to"]["pane"], ledger["to"]["name"]),
                                     (harness, sid, "w1:pX", address))
                    if harness == "claude":
                        self.assertEqual((pending["state"], pending["receipt"]),
                                         ("received", "exact-peer-ref"))
                    if harness == "codex":
                        self.assertEqual(pending["state"], "queued")
                    if harness == "opencode":
                        duty = peer_steward.peer_obligations.ObligationStore().get(
                            "message-" + ledger["transfer_ref"])
                        identity = duty["intent"]["identity"]
                        self.assertEqual((identity["server"], identity["pane"],
                                          identity["session_id"], duty["intent"]["target"]),
                                         ("selected", "w1:pX", sid, "w1:pX"))
                if harness == "claude":
                    unreceived = records[-1]
                    pending = peer_steward.peer_message._read_pending(unreceived["transfer_ref"])
                    self.assertEqual(pending["state"], "queued")
                    self.assertNotEqual(pending.get("receipt"), "exact-peer-ref")
                self.assertTrue(calls)
                self.assertTrue(all(argv[:3] == ["herdr", "--session", "selected"]
                                    for argv in calls), calls)
                if harness == "claude":
                    self.assertEqual(send.call_count, 4)
                else:
                    # OpenCode now proves the same identity and screen at send time;
                    # this fixture's open form still holds the row with no input.
                    send.assert_not_called()
                if harness == "codex":
                    self.assertEqual(codex_queue.call_count, 3)
                    self.assertIn("selected", self._codex_server_observations)
                if harness == "opencode":
                    self.assertEqual(native_status.call_count, 6)
                    self.assertTrue(all(call.args[0] == "selected"
                                        for call in native_status.call_args_list))
                if harness == "claude":
                    self.assertTrue(all(call.kwargs.get("pane") == "w1:pX"
                                        and call.kwargs.get("cwd") == "/fixture"
                                        for call in tag_lookup.call_args_list))

    def test_unresolved_full_prompt_retains_body_without_native_submission(self):
        self._readiness_patch.stop()
        peer_steward._HERDR_SESSION = "selected"

        cases = []
        for harness in ("claude", "codex", "opencode"):
            cases.append((harness, "absent"))
            cases.append((harness, "ambiguous"))
            cases.append((harness, "unknown"))
        for index, (harness, mode) in enumerate(cases):
            with self.subTest(harness=harness, mode=mode):
                if mode == "absent":
                    rows = []
                    self._set_inventory([])
                    target = "absent-" + harness
                elif mode == "ambiguous":
                    rows = [self._inventory_row(harness=harness, sid="sid-a", name="child",
                                                pane="w1:pA"),
                            self._inventory_row(harness=harness, sid="sid-b", name="child",
                                                pane="w1:pB")]
                    self._set_inventory(rows)
                    target = "child"
                else:
                    rows = []
                    self._census_panes = [{"pane_id": "w1:pKnown", "agent": harness,
                                           "agent_session": {"kind": "id", "value": "sid-known"}}]
                    target = "child"
                args = peer_steward.build_parser().parse_args(
                    ["prompt", target, "private body " + str(index)])
                before = len(self._all_records())
                with mock.patch.object(peer_steward.shutil, "which", return_value="/usr/bin/herdr"), \
                     mock.patch.object(peer_steward, "_prompt_agent_inventory",
                                       return_value=(rows, None)), \
                     mock.patch.object(peer_steward, "_retire_target", side_effect=self._target_for), \
                     mock.patch.object(peer_steward, "_retire_pane_info", side_effect=self._pane_info_for), \
                     mock.patch.object(peer_steward, "_current_session_identity",
                                       return_value=("sender-session", "claude")), \
                     mock.patch.object(peer_steward, "_from_name", return_value="sender"), \
                     mock.patch.object(peer_steward.peer_message, "prepare_peer_message",
                                       wraps=peer_steward.peer_message.prepare_peer_message) as prepare, \
                     mock.patch.object(peer_steward.peer_message, "deliver_pending_codex") as codex, \
                     mock.patch.object(peer_steward, "_herdr_prompt") as herdr_send, \
                     mock.patch.object(peer_steward, "_schedule_message_obligation") as schedule, \
                     mock.patch("builtins.print") as printed:
                    if mode == "unknown":
                        incomplete = SimpleNamespace(endpoint="selected", complete=False,
                                                     panes=tuple(self._census_panes))
                        with mock.patch.object(peer_steward.herdr_observation, "pane_census",
                                               return_value=incomplete):
                            rc = peer_steward.cmd_prompt(args)
                    else:
                        rc = peer_steward.cmd_prompt(args)
                self.assertEqual(rc, 5)
                self.assertNotIn("private body", printed.call_args[0][0])
                ledger = self._all_records()[before:][-1]
                self.assertNotIn("transfer_ref", ledger)
                self.assertEqual((ledger["to"]["name"], ledger["summary"],
                                  ledger["delivery"]["status"]), (target, "", "unknown"))
                if mode == "ambiguous":
                    diagnostics = json.loads(ledger["delivery"]["receipt"].split(":", 1)[1])
                    self.assertEqual(diagnostics["candidate_panes"], ["w1:pA", "w1:pB"])
                if mode == "unknown":
                    self.assertIn("w1:pKnown", ledger["delivery"]["receipt"])
                prepare.assert_called_once()
                codex.assert_not_called()
                herdr_send.assert_not_called()
                schedule.assert_called_once()
                row = peer_steward.peer_message._read_pending(schedule.call_args.args[0]["ref"])
                self.assertIn("private body " + str(index), row["text"])
                self.assertEqual(peer_steward.peer_obligations.ObligationStore().list(), [])

    def test_whole_prompt_rejects_sid_and_pane_replacement_and_pid_change(self):
        self._readiness_patch.stop()
        peer_steward._HERDR_SESSION = "selected"
        sid = "sid-original"
        payload = _agent_json("codex", sid, "child", status="idle")
        calls, queued = [], []
        current_calls = 0

        def target_after_resolution(pane):
            nonlocal current_calls
            current_calls += 1
            observed_sid = sid if current_calls <= 2 else "sid-replacement"
            self._native_sid_by_pid[os.getpid()] = observed_sid
            self._native_sid_by_harness["codex"] = observed_sid
            return "idle", {"harness": "codex", "session_id": observed_sid,
                             "name": "child", "pane": pane}, 0, None

        with mock.patch.object(peer_steward.subprocess, "run",
                               side_effect=self._fake_run(payload, calls)), \
             mock.patch.object(peer_steward, "_retire_target", side_effect=target_after_resolution), \
             mock.patch.object(peer_steward, "_current_session_identity",
                               return_value=("sender-session", "claude")), \
             mock.patch.object(peer_steward, "_from_name", return_value="sender"), \
             mock.patch.object(peer_steward, "_prompt_foreground",
                               return_value={"pid": os.getpid(), "start": "birth", "argv": ["codex"]}), \
             mock.patch.object(peer_steward.peer_message, "deliver_pending_codex",
                               side_effect=lambda *a, **kw: queued.append(a)), \
             mock.patch("builtins.print") as printed:
            rc = peer_steward.main(["prompt", "child", "[steer] pin the original SID"])
        self.assertEqual(rc, 5)
        self.assertIn("target-identity-changed", printed.call_args[0][0])
        self.assertEqual(queued, [])
        record = self._all_records()[-1]
        pending = peer_steward.peer_message._read_pending(record["transfer_ref"])
        self.assertEqual((pending["to"]["session_id"], pending["to"]["pane_id"]),
                         (sid, "w1:pX"))
        duty = peer_steward.peer_obligations.ObligationStore().get(
            "message-" + record["transfer_ref"])
        identity = duty["intent"]["identity"]
        self.assertEqual((identity["server"], identity["session_id"],
                          identity["pane"], duty["intent"]["target"]),
                         ("selected", sid, "w1:pX", "w1:pX"))

        # Claude's second foreground proof detects PID/birth replacement after
        # the exact recipient was resolved and before the native prompt call.
        claude_payload = _agent_json("claude", "sid-claude", "child", status="idle")
        calls = []
        proofs = iter(({"pid": 101, "start": "one", "pid_namespace": "pid:[1]",
                        "session_id": "sid-claude"},
                       {"pid": 102, "start": "two", "pid_namespace": "pid:[1]",
                        "session_id": "sid-claude"}))
        sends = []
        with mock.patch.object(peer_steward.subprocess, "run",
                               side_effect=self._fake_run(claude_payload, calls)), \
             mock.patch.object(peer_steward, "_current_session_identity",
                               return_value=("sender-session", "claude")), \
             mock.patch.object(peer_steward, "_from_name", return_value="sender"), \
             mock.patch.object(peer_steward, "_prompt_foreground",
                               return_value={"pid": os.getpid(), "start": "birth", "argv": ["claude"]}), \
             mock.patch.object(peer_steward, "_prompt_foreground_proof",
                               side_effect=lambda *_a, **_kw: next(proofs)), \
             mock.patch.object(peer_steward, "_read_screen",
                               return_value=peer_steward._screen_lines(CLAUDE_EMPTY)), \
             mock.patch.object(peer_steward, "_herdr_prompt",
                               side_effect=lambda *a, **kw: sends.append(a)), \
             mock.patch("builtins.print") as printed:
            rc = peer_steward.main(["prompt", "child", "[steer] keep the original process"])
        self.assertEqual(rc, 3)
        self.assertIn("target-process-changed", printed.call_args[0][0])
        self.assertEqual(sends, [])
        record = self._all_records()[-1]
        pending = peer_steward.peer_message._read_pending(record["transfer_ref"])
        self.assertEqual((pending["to"]["session_id"], pending["to"]["pane_id"]),
                         ("sid-claude", "w1:pX"))

    def test_deferred_resume_keeps_the_sealed_endpoint_pane_and_session(self):
        self._readiness_patch.stop()
        peer_steward._HERDR_SESSION = "selected"
        payload = _agent_json("opencode", "oc-original", "child", status="idle")
        calls = []

        def foreground(pane, kind, *, strict=True, info=None):
            return {"pid": os.getpid(), "start": "birth", "argv": [kind]}

        with mock.patch.object(peer_steward.subprocess, "run",
                               side_effect=self._fake_run(payload, calls)), \
             mock.patch.object(peer_steward, "_current_session_identity",
                               return_value=("sender-session", "claude")), \
             mock.patch.object(peer_steward, "_from_name", return_value="sender"), \
             mock.patch.object(peer_steward, "_prompt_foreground", side_effect=foreground), \
             mock.patch.object(peer_steward.herdr_observation, "read_native_status",
                               return_value={"status": "idle"}), \
             mock.patch("builtins.print"):
            self.assertEqual(peer_steward.main(
                ["prompt", "child", "[steer] defer on the original recipient"]), 3)
        record = self._all_records()[-1]
        ref = record["transfer_ref"]
        store = peer_steward.peer_obligations.ObligationStore()
        duty = store.get("message-" + ref)
        self.assertEqual(duty["intent"]["target"], "w1:pX")
        identity = duty["intent"]["identity"]
        self.assertEqual((identity["server"], identity["pane"],
                          identity["harness"], identity["session_id"]),
                         ("selected", "w1:pX", "opencode", "oc-original"))

        replacement = ("idle", {"harness": "opencode", "session_id": "oc-replacement",
                                 "name": "child", "pane": "w1:pReplacement"}, 0, None)
        with mock.patch.object(peer_steward, "_retire_target", return_value=replacement), \
             mock.patch.object(peer_steward.peer_message, "deliver_pending_codex") as codex:
            peer_steward._resume_message_obligation(duty, store)
        codex.assert_not_called()
        resumed = store.get(duty["id"])
        identity = resumed["intent"]["identity"]
        self.assertEqual((resumed["state"], identity["server"],
                          identity["pane"], identity["session_id"]),
                         ("unknown", "selected", "w1:pX", "oc-original"))
        self.assertEqual(resumed["observation"]["reason"], "target-identity-changed")
        pending = peer_steward.peer_message._read_pending(ref)
        self.assertEqual((pending["to"]["session_id"], pending["to"]["pane_id"]),
                         ("oc-original", "w1:pX"))

    def test_real_prompt_readiness_preserves_busy_form_and_registered_work_protections(self):
        self._readiness_patch.stop()
        peer_steward._HERDR_SESSION = "selected"
        from dispatch_attempt_policy import AttemptDecision, RegisteredWorkObservation

        for state, screen, with_work in (("working", CLAUDE_EMPTY, False),
                                         ("idle", CLAUDE_FORM, False),
                                         ("idle", CLAUDE_EMPTY, True)):
            with self.subTest(state=state, with_work=with_work):
                sid = "sid-guard-" + str(state) + str(with_work)
                payload = _agent_json("claude", sid, "child", status=state)
                calls, sends = [], []
                proof = {"pid": 101, "start": "birth", "pid_namespace": "pid:[1]",
                         "session_id": sid}

                def foreground(pane, harness, *, strict=True, info=None):
                    return {"pid": os.getpid(), "start": "birth", "argv": [harness]}

                patches = [
                    mock.patch.object(peer_steward.subprocess, "run",
                                      side_effect=self._fake_run(payload, calls)),
                    mock.patch.object(peer_steward, "_current_session_identity",
                                      return_value=("sender-session", "claude")),
                    mock.patch.object(peer_steward, "_from_name", return_value="sender"),
                    mock.patch.object(peer_steward, "_prompt_foreground", side_effect=foreground),
                    mock.patch.object(peer_steward, "_prompt_foreground_proof",
                                      return_value=proof),
                    mock.patch.object(peer_steward, "_read_screen",
                                      return_value=peer_steward._screen_lines(screen)),
                    mock.patch.object(peer_steward, "_herdr_prompt",
                                      side_effect=lambda *a, **kw: sends.append(a)),
                    mock.patch("builtins.print"),
                ]
                if with_work:
                    work = RegisteredWorkObservation(
                        "att-bound", AttemptDecision("pending", "wait", "owner", "worker-running"),
                        "running", "observed", owner_completion_state="pending",
                        cleanup_state="pending")
                    patches.append(mock.patch.object(
                        peer_steward.peer_obligations, "bound_work_for_pane",
                        return_value=((work,), "observed")))
                with contextlib.ExitStack() as stack:
                    for patcher in patches:
                        stack.enter_context(patcher)
                    rc = peer_steward.main([
                        "prompt", "child", "[steer] respect existing pane protections " + sid])
                self.assertEqual(rc, 3)
                self.assertEqual(sends, [])
                record = self._all_records()[-1]
                pending = peer_steward.peer_message._read_pending(record["transfer_ref"])
                self.assertEqual((pending["state"], pending["to"]["session_id"],
                                  pending["to"]["pane_id"]), ("pending", sid, "w1:pX"))
                duty = peer_steward.peer_obligations.ObligationStore().get(
                    "message-" + record["transfer_ref"])
                identity = duty["intent"]["identity"]
                self.assertEqual((identity["server"], identity["pane"],
                                  identity["session_id"]), ("selected", "w1:pX", sid))

    def test_pending_flush_only_uses_codex_native_queue(self):
        claude_text, claude_ref = peer_steward.peer_message.prepare_peer_message(
            "preserve until Claude callback", {"harness": "codex", "session_id": "sender"},
            {"harness": "claude", "session_id": "claude-target"}, defer=True)
        with mock.patch.object(peer_steward.peer_message, "deliver_pending_codex") as queue, \
             mock.patch.object(peer_steward, "_herdr_prompt", side_effect=AssertionError("PTY prompt")):
            self.assertEqual(peer_steward._flush_pending_for_target(
                "claude-target", "claude", "claude-target", "idle")[0], 0)
        queue.assert_not_called()
        self.assertEqual(peer_steward.peer_message._read_pending(claude_ref)["text"], claude_text)

        _text, codex_ref = peer_steward.peer_message.prepare_peer_message(
            "exact Codex queued message", {"harness": "claude", "session_id": "sender"},
            {"harness": "codex", "session_id": "thread"}, defer=True)
        with mock.patch.object(peer_steward, "_resolve_target", return_value=("codex", "thread", "child")), \
             mock.patch.object(peer_steward.peer_message, "deliver_pending_codex",
                               return_value={"status": "queued"}) as queue:
            self.assertEqual(peer_steward._flush_pending_for_target(
                "child", "codex", "thread", "idle"), (0, []))
        queue.assert_called_once_with(codex_ref, timeout=0.25, start_interrupted=False)

    def test_claude_prompt_guard_accepts_a_finished_turn_but_not_work_or_questions(self):
        ident = {"harness": "claude", "session_id": "sid-1", "pane": "w1:p1"}
        proof = {"pid": 1, "start": "2", "pid_namespace": "pid:[1]", "session_id": "sid-1"}
        ready = mock.Mock(state="ready", reason=None)
        for state, expected in (("idle", None), ("done", None), ("working", "native-state-working"),
                                ("blocked", "native-state-blocked")):
            with self.subTest(state=state), \
                 mock.patch.object(peer_steward, "_retire_target", return_value=(state, ident, 0, None)), \
                 mock.patch.object(peer_steward, "_prompt_current_candidate",
                                   return_value=({"_native_session_id": "sid-1"}, None)), \
                 mock.patch.object(peer_steward, "_agent_state", return_value=(state, "w1:p1")), \
                 mock.patch.object(peer_steward, "_pane_readiness", return_value=ready), \
                 mock.patch.object(peer_steward, "_prompt_foreground_proof", return_value=proof), \
                 mock.patch.object(peer_steward, "_read_screen", return_value=["> "]), \
                 mock.patch.object(peer_steward, "_screen_ready", return_value=None):
                reason, got = peer_steward._native_idle_prompt_guard("w1:p1", "claude", "sid-1", "w1:p1")
                self.assertEqual(reason, expected)
                self.assertEqual(got, proof if expected is None else None)

    def test_source_has_no_general_prompt_observation_or_no_verify_bypass(self):
        source = (_HERE / "../utilities/peer-steward.py").resolve().read_text()
        start = source.index("def cmd_prompt(")
        # The function itself: the "# --- clear:" section that followed it moved to
        # peer_steward_duties.py when this file was split.
        import ast
        prompt = next(ast.get_source_segment(source, node) for node in ast.parse(source).body
                      if isinstance(node, ast.FunctionDef) and node.name == "cmd_prompt")
        for obsolete in ("_prompt_box", "_verify_after_send", "_transcript_arrival",
                         "_prompt_input_reason", "no_verify", "state-flip"):
            self.assertNotIn(obsolete, prompt)
        self.assertIn("elif t_harness in {\"claude\", \"opencode\"}", prompt)
        self.assertIn("_submit_pending_native", prompt)
        self.assertIn("_native_idle_prompt_guard", source[source.index("def _submit_pending_native"):start])
        self.assertNotIn("--no-verify", peer_steward.build_parser().format_help())


class RetainedRunNoticeCarrierTest(_TmpRootMixin, unittest.TestCase):
    def parent_row(self, sid):
        from route_identity import route_hash
        route = {"route_id": "rt-notice-parent", "nodes": [{"id": "execute"}]}
        route["route_hash"] = route_hash(route)
        path = self.tmp_root / "notice-parent.json"
        path.write_text(json.dumps(route))
        metadata = {"attempt_schema_version": "2", "attempt_id": "att-owner", "parent_sid": sid, "parent_harness": "claude",
            "parent_completion_delivery": "claude-parent-runtime", "dispatch_depth": "1",
            "worker_type": "owner", "registered_worker": "1", "harness": "claude",
            "route_file": str(path), "route_id": route["route_id"],
            "route_hash": route["route_hash"], "route_node": "execute"}
        self.jobs_path.write_text("now\trunning\t/fixture\t/fixture\towner\t" +
            ",".join(key + "=" + value for key, value in metadata.items()) + "\n")

    def succession(self, origin, target):
        import session_tidy
        session_tidy._append_ledger_entry(session_tidy.Seat("pane", "retained-run-notice"),
            {"event": "succession", "source": "tidy-clear", "harness": "claude",
             "from": origin, "sid": target, "ts": 1})

    def setUp(self):
        super().setUp()
        self.sid = "fixture-owner-session"
        self.pane = "w1:p-notice"
        self.ident = {"pane": self.pane, "harness": "claude", "session_id": self.sid,
                      "pid": os.getpid(), "pid_start": peer_steward._proc_start_ticks(os.getpid()),
                      "pid_namespace": os.readlink("/proc/self/ns/pid")}

    def _notice(self, harness, key="checkpoint:run-1:slot-1"):
        import session_notice
        with mock.patch.object(peer_steward.peer_obligations, "ensure_normal_runner",
                               return_value=False), \
                mock.patch.object(peer_steward.peer_obligations,
                                  "request_dispatch_reconciliation", return_value=False):
            return session_notice.notify(harness, self.sid, key=key,
                subject="experiment checkpoint", text="run=run-1; ETA total 2h",
                route_id="route-fixture", jobs=self.jobs_path)

    def _native_address_notice(self, harness, *, changed=False):
        import session_shell
        import dispatch_pending_delivery as pending_delivery
        notice = self._notice(harness)
        store = peer_steward.peer_obligations.ObligationStore(self.tmp_root)
        duty, = store.list(kinds={"run-notice"})
        attachment = {"harness": harness, "sid": self.sid,
                      "native_pid": os.getpid(), "native_start": self.ident["pid_start"]}
        observations = []
        def attached(*args):
            observations.append(args)
            return (None, "attachment-changed") if changed and len(observations) > 1 else (attachment, "")
        sends = []
        def receive(args, *, expected_recipient, on_prepared, producer_binding):
            self.assertEqual(args.target, harness + ":" + self.sid)
            self.assertEqual(expected_recipient, (harness, self.sid))
            body = Path(args.body_file).read_text()
            text, ref = peer_steward.peer_message.prepare_peer_message(body,
                {"harness": "runtime", "session_id": None, "name": "Hearting runtime"},
                {"harness": harness, "session_id": self.sid}, defer=True,
                refs=args.ref, producer_binding=producer_binding)
            on_prepared(ref)
            sends.append(ref)
            self.assertEqual(peer_steward.peer_message.receive_peer_message(
                text, {"harness": harness, "session_id": self.sid}), 0)
            return 0
        with mock.patch.object(session_shell, "exact_binding", side_effect=attached), \
             mock.patch.object(peer_steward.subprocess, "run", side_effect=OSError("no terminal multiplexer")) as external, \
             mock.patch.object(peer_steward, "cmd_prompt", side_effect=receive) as prompt:
            peer_steward._resume_session_notice_obligations([duty], store)
            if changed or harness == "opencode":
                # Ordinary OpenCode run notices retain their native plugin owner;
                # message-question answers use the answer-binding path (PTY proof).
                prompt.assert_not_called()
                self.assertEqual(pending_delivery.read(self.tmp_root, self.sid, notice["delivery_id"])["state"], "pending")
            else:
                self.assertEqual(len(sends), 1)
                peer_steward._resume_session_notice_obligations([store.get(duty["id"])], store)
                self.assertEqual(len(sends), 1)
                self.assertEqual(pending_delivery.read(self.tmp_root, self.sid, notice["delivery_id"])["state"], "acked")
            external.assert_not_called()

    def test_claude_notice_uses_the_exact_native_address_without_herdr(self):
        self._native_address_notice("claude")

    def test_codex_notice_uses_the_exact_native_address_without_herdr(self):
        self._native_address_notice("codex")

    def test_opencode_ordinary_notice_keeps_its_native_carrier(self):
        self._native_address_notice("opencode")

    def test_changed_shell_attachment_cannot_claim_or_prepare_a_notice(self):
        self._native_address_notice("claude", changed=True)

    def _run_notice_duty(self, harness, duty, *, fail_ack=False, forget_binding=False,
                         target_state="idle", target_sid=None,
                         queue_accept_only=False, duties=None, idle_guard=None, input_observer=None,
                         native_queue_impl=None):
        import dispatch_pending_delivery as pending_delivery
        target_sid = target_sid or self.sid
        target_ident = {**self.ident, "harness": harness, "session_id": target_sid}
        store = peer_steward.peer_obligations.ObligationStore(self.tmp_root)
        agents = {"result": {"agents": [{"agent": harness,
            "agent_session": {"value": target_sid}, "pane_id": self.pane}]}}
        resolved = {"pane": self.pane, "harness": harness, "session_id": target_sid,
                    "name": "fixture-owner", "state": target_state, "_identity": target_ident}
        sends = []
        def native_receive(_pane, body, *, wait, timeout_ms, start_aware=False):
            self.assertFalse(wait)
            if input_observer:
                input_observer(harness, target_sid, body)
            sends.append(body)
            if fail_ack:
                with mock.patch.object(pending_delivery, "ack_received",
                                       side_effect=OSError("fixture ack failure")):
                    rc = peer_steward.peer_message.receive_peer_message(
                        body, {"harness": harness, "session_id": target_sid})
            elif forget_binding:
                with mock.patch.object(peer_steward.peer_message, "_ack_registered_delivery",
                                       return_value=False):
                    rc = peer_steward.peer_message.receive_peer_message(
                        body, {"harness": harness, "session_id": target_sid})
            else:
                rc = peer_steward.peer_message.receive_peer_message(
                    body, {"harness": harness, "session_id": target_sid})
            self.assertEqual(rc, 0)
            return 0, {}
        def native_queue(ref, *, timeout=1.0, **_kw):
            row = peer_steward.peer_message._read_pending(ref)
            self.assertIsNotNone(row)
            sends.append(row["text"])
            if queue_accept_only:
                with peer_steward.peer_message.pending_lock(ref):
                    current = peer_steward.peer_message._read_pending(ref)
                    peer_steward.peer_message._save_pending(dict(
                        current, state="queued", receipt="native-queue-accepted"))
                return {"status": "queued", "reason": "fixture-native-queue-accepted"}
            if input_observer:
                input_observer(harness, target_sid, row["text"])
            rc = peer_steward.peer_message.receive_peer_message(
                row["text"], {"harness": harness, "session_id": target_sid})
            self.assertEqual(rc, 0)
            return {"status": "received", "reason": "fixture-exact-native-history"}
        result = subprocess.CompletedProcess([], 0, json.dumps(agents), "")
        ready = (SimpleNamespace(state="ready", reason="")
                 if target_state == "idle" or harness == "codex"
                 else SimpleNamespace(state="unknown", reason="native-state-working"))
        with mock.patch.object(peer_steward.subprocess, "run", return_value=result), \
                mock.patch.object(peer_steward.shutil, "which", return_value="/usr/bin/herdr"), \
                mock.patch.object(peer_steward, "_resolve_target",
                                  return_value=(harness, target_sid, "fixture-owner")), \
                mock.patch.object(peer_steward, "_resolve_prompt_target",
                                  return_value=(resolved, None, [])), \
                mock.patch.object(peer_steward, "_pane_readiness", return_value=ready), \
                mock.patch.object(peer_steward, "_prompt_current_candidate",
                                  return_value=({"state": "idle", "_identity": target_ident}, None)), \
                mock.patch.object(peer_steward, "_current_session_identity",
                                  return_value=("fixture-steward", "claude")), \
                mock.patch.object(peer_steward, "_from_name", return_value="fixture-steward"), \
                mock.patch.object(peer_steward, "_project_of", return_value=str(self.tmp_root)), \
                mock.patch.object(peer_steward, "_native_idle_prompt_guard",
                                  side_effect=idle_guard or (lambda *a, **k: (None, "fixture-foreground-proof"))), \
                mock.patch.object(peer_steward, "_prompt_foreground_proof",
                                  return_value={"pid": os.getpid(), "start": "fixture-start",
                                      "pid_namespace": self.ident["pid_namespace"],
                                      "session_id": target_sid}), \
                mock.patch.object(peer_steward, "_read_screen",
                                  return_value=peer_steward._screen_lines(CLAUDE_SUGGESTION)), \
                mock.patch.object(peer_steward, "_herdr_prompt", side_effect=native_receive), \
                mock.patch.object(peer_steward.peer_message, "deliver_pending_codex",
                                  side_effect=native_queue_impl or native_queue):
            peer_steward._reconcile_obligations(
                store, duties=peer_steward._runner_duties(store, duties or [duty]))
        return store, sends

    def _resume_claude_message_duty(self, duty, store):
        ready = SimpleNamespace(state="ready", reason="")
        def receive(_pane, body, *, wait, timeout_ms, start_aware=False):
            self.assertFalse(wait)
            self.assertEqual(peer_steward.peer_message.receive_peer_message(
                body, {"harness": "claude", "session_id": self.sid}), 0)
            return 0, {}
        resolved = {"pane": self.pane, "harness": "claude", "session_id": self.sid,
                    "name": "fixture-owner", "state": "idle", "_identity": self.ident}
        with mock.patch.object(peer_steward.subprocess, "run",
                               return_value=subprocess.CompletedProcess([], 0, "{}", "")), \
                mock.patch.object(peer_steward.shutil, "which", return_value="/usr/bin/herdr"), \
                mock.patch.object(peer_steward, "_retire_target",
                                  return_value=("idle", self.ident, 0, None)), \
                mock.patch.object(peer_steward, "_resolve_target",
                                  return_value=("claude", self.sid, "fixture-owner")), \
                mock.patch.object(peer_steward, "_resolve_prompt_target",
                                  return_value=(resolved, None, [])), \
                mock.patch.object(peer_steward, "_prompt_current_candidate",
                                  return_value=({"state": "idle", "_identity": self.ident}, None)), \
                mock.patch.object(peer_steward, "_pane_readiness", return_value=ready), \
                mock.patch.object(peer_steward, "_native_idle_prompt_guard",
                                  return_value=(None, "fixture-foreground-proof")), \
                mock.patch.object(peer_steward, "_prompt_foreground_proof",
                                  return_value={"pid": os.getpid(), "start": "fixture-start",
                                      "pid_namespace": self.ident["pid_namespace"],
                                      "session_id": self.sid}), \
                mock.patch.object(peer_steward, "_read_screen",
                                  return_value=peer_steward._screen_lines(CLAUDE_SUGGESTION)), \
                mock.patch.object(peer_steward, "_herdr_prompt", side_effect=receive):
            peer_steward._resume_message_obligation(duty, store)

    def test_claude_and_codex_native_receipts_ack_the_same_retained_notice(self):
        for harness in ("claude", "codex"):
            with self.subTest(harness=harness):
                notice = self._notice(harness, key="checkpoint:" + harness)
                self.assertEqual(notice["attempt_ids"], [])
                duplicate = self._notice(harness, key="checkpoint:" + harness)
                self.assertEqual(duplicate["delivery_id"], notice["delivery_id"])
                store = peer_steward.peer_obligations.ObligationStore(self.tmp_root)
                duty, = [item for item in store.list(kinds={"run-notice"})
                         if item["intent"]["delivery_id"] == notice["delivery_id"]]
                store, sends = self._run_notice_duty(harness, duty)
                import dispatch_pending_delivery as pending_delivery
                current = pending_delivery.read(
                    self.tmp_root, self.sid, notice["delivery_id"])
                self.assertEqual(current["state"], "acked")
                self.assertEqual(len(sends), 1)
                self.assertEqual(store.get(duty["id"])["state"], "complete")

    def test_codex_queue_acceptance_stays_unacked_until_exact_native_receipt(self):
        import dispatch_pending_delivery as pending_delivery
        notice = self._notice("codex", key="checkpoint:queue-then-receipt")
        store = peer_steward.peer_obligations.ObligationStore(self.tmp_root)
        duty, = store.list(kinds={"run-notice"})
        store, sends = self._run_notice_duty("codex", duty, queue_accept_only=True,
                                             target_state="working")
        current = pending_delivery.read(self.tmp_root, self.sid, notice["delivery_id"])
        self.assertEqual(current["state"], "sent-ambiguous")
        transfer_ref = store.get(duty["id"])["observation"]["transfer_ref"]
        transfer = peer_steward.peer_message._read_pending(transfer_ref)
        self.assertEqual(transfer["state"], "queued")
        self.assertEqual(len(sends), 1)
        self.assertEqual(peer_steward.peer_message.receive_peer_message(
            transfer["text"], {"harness": "codex", "session_id": self.sid}), 0)
        refreshed = store.get(duty["id"])
        peer_steward._reconcile_obligations(
            store, duties=peer_steward._runner_duties(store, [refreshed]))
        self.assertEqual(pending_delivery.read(
            self.tmp_root, self.sid, notice["delivery_id"])["state"], "acked")
        self.assertEqual(store.get(duty["id"])["state"], "complete")

    def test_ack_retry_preserves_consumed_receipt_without_resending(self):
        notice = self._notice("claude", key="attention:run-1:nan")
        store = peer_steward.peer_obligations.ObligationStore(self.tmp_root)
        duty, = store.list(kinds={"run-notice"})
        store, sends = self._run_notice_duty("claude", duty, fail_ack=True)
        import dispatch_pending_delivery as pending_delivery
        transfer_ref = store.get(duty["id"])["observation"]["transfer_ref"]
        transfer = peer_steward.peer_message._read_pending(transfer_ref)
        self.assertEqual(transfer["state"], "received")
        pending_before = pending_delivery.read(
            self.tmp_root, self.sid, notice["delivery_id"])
        self.assertEqual(pending_before["state"], "sent-ambiguous")
        refreshed = store.get(duty["id"])
        with mock.patch.object(peer_steward.subprocess, "run",
                               return_value=subprocess.CompletedProcess([], 0, "{}", "")):
            peer_steward._reconcile_obligations(
                store, duties=peer_steward._runner_duties(store, [refreshed]))
        pending_after = pending_delivery.read(
            self.tmp_root, self.sid, notice["delivery_id"])
        self.assertEqual(pending_after["state"], "acked")
        self.assertEqual(len(sends), 1)
        self.assertEqual(store.get(duty["id"])["state"], "complete")

    def test_busy_claude_recipient_is_delivered_by_the_existing_runner_when_idle(self):
        import dispatch_pending_delivery as pending_delivery
        notice = self._notice("claude", key="checkpoint:busy-then-idle")
        store = peer_steward.peer_obligations.ObligationStore(self.tmp_root)
        duty, = store.list(kinds={"run-notice"})
        store, sends = self._run_notice_duty("claude", duty, target_state="working")
        self.assertEqual(sends, [])
        self.assertEqual(pending_delivery.read(
            self.tmp_root, self.sid, notice["delivery_id"])["state"], "sent-ambiguous")
        transfer_ref = store.get(duty["id"])["observation"]["transfer_ref"]
        message_duty = store.get("message-" + transfer_ref)
        self.assertIsNotNone(message_duty)
        self._resume_claude_message_duty(message_duty, store)
        self.assertEqual(pending_delivery.read(
            self.tmp_root, self.sid, notice["delivery_id"])["state"], "acked")
        self.assertEqual(store.get(duty["id"])["state"], "complete")

    def test_received_but_unbound_history_is_not_resent(self):
        notice = self._notice("claude", key="checkpoint:unknown-history")
        store = peer_steward.peer_obligations.ObligationStore(self.tmp_root)
        duty, = store.list(kinds={"run-notice"})
        store, sends = self._run_notice_duty("claude", duty, forget_binding=True)
        current = store.get(duty["id"])
        transfer_ref = current["observation"]["transfer_ref"]
        transfer = peer_steward.peer_message._read_pending(transfer_ref)
        self.assertEqual((transfer["state"], transfer["receipt"]),
                         ("received", "exact-peer-ref"))
        observation = dict(current["observation"])
        observation["transfer_bindings"] = {}
        current = store.update(duty["id"], observation=observation)
        peer_steward._reconcile_obligations(
            store, duties=peer_steward._runner_duties(store, [current]))
        self.assertEqual(store.get(duty["id"])["observation"]["reason"],
                         "received-notice-history-unbound")
        self.assertEqual(len(sends), 1)
        import dispatch_pending_delivery as pending_delivery
        self.assertEqual(pending_delivery.read(
            self.tmp_root, self.sid, notice["delivery_id"])["state"], "sent-ambiguous")

    def test_referenced_group_busy_retry_keeps_original_body_ref_and_receipts(self):
        import session_notice
        import dispatch_pending_delivery as pending
        peer = peer_steward.peer_message
        records = [session_notice.notify("claude", self.sid, key="long-checkpoint-" + str(index),
            subject="experiment checkpoint", text="metric=0.5; " + "observed " * 60,
            jobs=self.jobs_path) for index in range(8)]
        store = peer_steward.peer_obligations.ObligationStore(self.tmp_root)
        duties = store.list(kinds={"run-notice"})
        with mock.patch.object(peer_steward, "_peer_reference_access_evidence", return_value={"fixture": True}), \
             mock.patch.object(peer, "_reference_access_evidence_valid", return_value=True):
            store, sends = self._run_notice_duty("claude", duties[0], duties=duties, target_state="working")
        self.assertEqual(sends, [])
        ref = store.get(duties[0]["id"])["observation"]["transfer_ref"]
        transfer = peer._read_pending(ref)
        self.assertIsNotNone(transfer.get("body_ref"))
        original_payload = peer.read_peer_payload(ref)["body"]
        self.assertEqual(original_payload.count("HEARTING_RECEIPT_V1 "), 8)
        seal = peer._transfer_path(ref).read_bytes()
        self._resume_claude_message_duty(store.get("message-" + ref), store)
        self.assertEqual(peer._read_pending(ref)["state"], "received")
        self.assertEqual(peer.read_peer_payload(ref)["body"], original_payload)
        self.assertEqual(peer._transfer_path(ref).read_bytes(), seal)
        self.assertEqual(len(list(peer._transfer_path(ref).parent.glob("*.json"))), 1)
        self.assertTrue(all(pending.read(self.tmp_root, self.sid, record["delivery_id"])["state"] == "acked"
                            for record in records))

    def test_parent_handover_refreshes_recipient_without_rekeying_notice(self):
        import dispatch_completion_join as join
        import dispatch_pending_delivery as pending_delivery
        import dispatch_seat_handover as handover
        import session_notice
        original_sid, successor_sid = "sid-parent-old", "sid-parent-new"
        self.parent_row(original_sid)
        lineage = {"kind": "attempt-parent", "attempt_id": "att-owner",
                   "harness": "claude", "session_id": original_sid}
        with mock.patch.object(peer_steward.peer_obligations, "ensure_normal_runner",
                               return_value=False), \
                mock.patch.object(peer_steward.peer_obligations,
                                  "request_dispatch_reconciliation", return_value=False):
            notice = session_notice.notify("claude", original_sid, key="final:handover",
                subject="experiment final", text="run=run-3; completed",
                jobs=self.jobs_path, lineage=lineage)
        identity = {"jobs": str(self.jobs_path), "storage_recipient": original_sid,
                    "recipient_harness": "claude", "recipient_session_id": original_sid,
                    "lineage": lineage}
        first = peer_steward.peer_obligations._ordinary_notice_target(
            identity, "claude-parent-runtime")
        self.succession(original_sid, successor_sid)
        second = peer_steward.peer_obligations._ordinary_notice_target(
            identity, "claude-parent-runtime")
        before = pending_delivery.read(self.tmp_root, original_sid, notice["delivery_id"])
        frozen = pending_delivery.received_ack_identity(before, original_sid, notice["delivery_id"])
        after = pending_delivery.read(self.tmp_root, original_sid, notice["delivery_id"])
        self.assertEqual(first, ("claude", original_sid, original_sid))
        self.assertEqual(second, ("claude", successor_sid, original_sid))
        self.assertEqual(after["delivery_id"], notice["delivery_id"])
        self.assertEqual(after["receipt_digest"], notice["receipt_digest"])
        self.assertEqual(pending_delivery.received_ack_identity(
            after, original_sid, notice["delivery_id"]), frozen)

    def test_handover_send_uses_successor_session_and_acks_under_original_storage_key(self):
        import dispatch_pending_delivery as pending_delivery
        import session_notice
        original_sid, successor_sid = "sid-parent-old", "sid-parent-new"
        self.parent_row(original_sid)
        self.succession(original_sid, successor_sid)
        self.sid = original_sid
        self.ident = {**self.ident, "session_id": original_sid}
        lineage = {"kind": "attempt-parent", "attempt_id": "att-owner",
                   "harness": "claude", "session_id": original_sid}
        with mock.patch.object(peer_steward.peer_obligations, "ensure_normal_runner",
                               return_value=False), \
                mock.patch.object(peer_steward.peer_obligations,
                                  "request_dispatch_reconciliation", return_value=False):
            notice = session_notice.notify("claude", original_sid, key="checkpoint:handed-over",
                subject="experiment checkpoint", text="run=run-4; ETA total 3h",
                jobs=self.jobs_path, lineage=lineage)
        before = pending_delivery.read(self.tmp_root, original_sid, notice["delivery_id"])
        frozen = pending_delivery.received_ack_identity(before, original_sid, notice["delivery_id"])
        store = peer_steward.peer_obligations.ObligationStore(self.tmp_root)
        duty, = store.list(kinds={"run-notice"})
        store, sends = self._run_notice_duty("claude", duty, target_sid=successor_sid)
        after = pending_delivery.read(self.tmp_root, original_sid, notice["delivery_id"])
        self.assertEqual(len(sends), 1)
        transfer_ref = store.get(duty["id"])["observation"]["transfer_ref"]
        transfer = peer_steward.peer_message._read_pending(transfer_ref)
        self.assertEqual(transfer["to"]["session_id"], successor_sid)
        self.assertEqual(after["state"], "acked")
        self.assertEqual(after["delivery_id"], notice["delivery_id"])
        self.assertEqual(after["receipt_digest"], notice["receipt_digest"])
        self.assertEqual(pending_delivery.received_ack_identity(
            after, original_sid, notice["delivery_id"]), frozen)
        self.assertEqual(store.get(duty["id"])["state"], "complete")


class ObligationStartupTest(_TmpRootMixin, unittest.TestCase):
    def test_external_duties_keep_the_runner_lease_until_they_settle(self):
        store = peer_steward.peer_obligations.ObligationStore()
        store.root.mkdir(parents=True, exist_ok=True)
        passes = []
        real_flock = fcntl.flock
        with (store.root / peer_steward.peer_obligations.RUNNER_LOCK_NAME).open("w") as lock:
            real_flock(lock, fcntl.LOCK_EX)
            def reconcile(*args, **kwargs):
                passes.append(len(passes) + 1)
            def signature(_snapshot=None):
                return ((("watch-still-owned",),), ()) if len(passes) < 3 else ((), ())
            def flock(fd, operation):
                if fd == lock.fileno() and operation == fcntl.LOCK_UN:
                    self.assertGreaterEqual(len(passes), 3,
                        "a callback could otherwise take this lease and discard the live owner's caches")
                return real_flock(fd, operation)
            with patch_split(peer_steward, "_external_obligation_snapshot", return_value={}), \
                 mock.patch.object(peer_steward, "_external_obligation_signature", side_effect=signature), \
                 mock.patch.object(peer_steward, "_external_obligation_snapshot_unchanged", return_value=True), \
                 mock.patch.object(peer_steward, "_reconcile_external_obligations", side_effect=reconcile), \
                 mock.patch.object(peer_steward.fcntl, "flock", side_effect=flock), \
                 mock.patch.object(peer_steward.time, "sleep") as sleep:
                self.assertEqual(peer_steward.cmd_obligation_runner(
                    SimpleNamespace(state_root=self.tmp_root, lock_fd=lock.fileno())), 0)
            self.assertEqual(passes, [1, 2, 3])
            self.assertEqual(sleep.call_count, 2)

    def test_actual_runner_rotates_blocked_notice_and_delivers_later_once(self):
        import dispatch_pending_delivery as pending
        import session_notice

        with mock.patch.object(peer_steward.peer_obligations,
                               "ensure_normal_runner", return_value=False):
            blocked = _legacy_notice(
                "claude", "sid-a-storage", key="runner-blocked",
                subject="blocked notice", text="keep original recipient key", jobs=self.jobs_path)
            blocked_retry = _legacy_notice(
                "claude", "sid-a-storage", key="runner-blocked",
                subject="blocked notice", text="keep original recipient key", jobs=self.jobs_path)
            later = _legacy_notice(
                "claude", "sid-z-storage", key="runner-later",
                subject="later notice", text="deliver through fixture courier", jobs=self.jobs_path)

        self.assertEqual(blocked["delivery_id"], blocked_retry["delivery_id"])
        store = peer_steward.peer_obligations.ObligationStore(self.tmp_root)
        lock_path = store.root / peer_steward.peer_obligations.RUNNER_LOCK_NAME
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        blocked_path = pending.record_path(self.tmp_root, "sid-a-storage",
                                           blocked["delivery_id"])
        blocked_before = (blocked_path.read_bytes(), blocked_path.stat().st_mtime_ns)
        deliveries = []
        waits = []
        inventory = {"result": {"agents": [{
            "agent": "claude", "agent_session": {"value": "sid-z-live"},
            "pane_id": "pane-z-live",
        }]}}

        def courier(args, *, expected_recipient, on_prepared, producer_binding=None):
            deliveries.append({
                "body": Path(args.body_file).read_text(encoding="utf-8"),
                "recipient": expected_recipient,
                "refs": list(args.ref),
                "producer": producer_binding,
            })
            on_prepared("transfer-z-live")
            return 0

        def stop_after_four_bounded_passes(_delay):
            waits.append(_delay)
            if len(waits) == 4:
                # This lock belongs only to this fake root. Its removal makes the
                # real runner take its existing replaced-root exit path next pass.
                lock_path.unlink()

        def recipient_for_storage(_record, storage_sid, *, harness, jobs=None, read_context=None):
            return (harness, {"sid-a-storage": "sid-a-blocked",
                    "sid-z-storage": "sid-z-live"}[storage_sid])

        with lock_path.open("w", encoding="utf-8") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            with mock.patch.object(peer_steward, "_herdr_argv",
                                   return_value=["herdr", "agent", "list"]), \
                 mock.patch.object(peer_steward.subprocess, "run", return_value=SimpleNamespace(
                     stdout=json.dumps(inventory), returncode=0)), \
                 mock.patch("dispatch_seat_handover.record_recipient_binding",
                            side_effect=recipient_for_storage), \
                 mock.patch.object(peer_steward, "_resolve_target",
                                   return_value=("claude", "sid-z-live", "fixture")), \
                 mock.patch.object(peer_steward.peer_message, "_read_pending",
                                   return_value={"state": "queued"}) as transfer_lookup, \
                 mock.patch.object(peer_steward, "cmd_prompt", side_effect=courier), \
                 patch_split(peer_steward, "_external_obligation_snapshot",
                                   return_value={}), \
                 mock.patch.object(peer_steward, "_external_obligation_signature",
                                   return_value=()), \
                 mock.patch.object(peer_steward,
                                   "_external_obligation_snapshot_unchanged",
                                   return_value=True), \
                 mock.patch.object(peer_steward, "_reconcile_external_obligations"), \
                 mock.patch.object(peer_steward, "_publish_watch_readiness"), \
                 mock.patch.object(peer_steward, "_runner_readiness_fds", return_value=[]), \
                 mock.patch.object(peer_steward.time, "sleep",
                                   side_effect=stop_after_four_bounded_passes):
                self.assertEqual(peer_steward.cmd_obligation_runner(SimpleNamespace(
                    state_root=self.tmp_root, lock_fd=lock.fileno())), 0)

        self.assertEqual(len(waits), 4, "the actual runner completed four bounded passes")
        transfer_lookup.assert_called_once_with(
            "transfer-z-live", roots=[self.tmp_root], state_root=self.tmp_root)
        self.assertEqual(len(deliveries), 1, "queued fixture delivery is never sent twice")
        self.assertEqual(deliveries[0]["recipient"], ("claude", "sid-z-live"))
        self.assertIn("later notice: deliver through fixture courier", deliveries[0]["body"])
        self.assertEqual(deliveries[0]["producer"]["recipient"], {
            "harness": "claude", "session_id": "sid-z-live"})
        self.assertEqual(deliveries[0]["producer"]["records"][0]["storage_sid"],
                         "sid-z-storage")

        duties = store.list(kinds={"session-notice"})
        by_storage = {duty["intent"]["identity"]["storage_sid"]: duty for duty in duties}
        self.assertEqual(by_storage["sid-a-storage"]["state"], "pending")
        self.assertEqual(by_storage["sid-a-storage"]["observation"]["reason"],
                         "recipient-unavailable")
        self.assertEqual(by_storage["sid-z-storage"]["state"], "pending")
        self.assertEqual(by_storage["sid-z-storage"]["observation"]["transfer_ref"],
                         "transfer-z-live")
        self.assertEqual((blocked_path.read_bytes(), blocked_path.stat().st_mtime_ns),
                         blocked_before)
        stored_later = pending.read(self.tmp_root, "sid-z-storage", later["delivery_id"])
        self.assertEqual(stored_later["recipient_digest"],
                         pending.recipient_digest("sid-z-storage"))
        self.assertEqual(stored_later["receipt_digest"], later["receipt_digest"])
        self.assertFalse(pending.record_path(
            self.tmp_root, "sid-z-live", later["delivery_id"]).exists())

    def pending_message(self):
        store = peer_steward.peer_obligations.ObligationStore()
        duty = store.create("message-fixture", "message",
                            {"harness": "codex", "session_id": "sid-fixture"}, {})
        return store, duty

    def assert_runner_request(self, runner, lifecycle_root):
        runner.assert_called_once()
        args, kwargs = runner.call_args
        self.assertEqual(args, ())
        self.assertEqual(kwargs["lifecycle_root"], lifecycle_root)
        self.assertTrue(kwargs["reconciliation_required"])
        self.assertIsInstance(kwargs["_store"],
                              peer_steward.peer_obligations.ObligationStore)
        self.assertIsInstance(kwargs["_recovery_lock_fd"], int)

    def test_shared_recovery_lock_is_short_noninheritable_and_per_root(self):
        shared_peer_root = self.tmp_root / "peer-shared"
        alternate_peer_root = self.tmp_root / "peer-independent"
        shared_peer_root.mkdir()
        alternate_peer_root.mkdir()
        lifecycle_root = self.tmp_root / "lifecycle"
        lifecycle_root.mkdir()
        jobs = self.tmp_root / "jobs.log"
        jobs.touch()
        with mock.patch.dict(os.environ, {"AGENT_PEER_LEDGER_ROOT": str(shared_peer_root)}):
            store = peer_steward.peer_obligations.ObligationStore()
            pending = store.create("message-pending", "message",
                                   {"harness": "codex", "session_id": "recipient-pending"},
                                   {"attempt_ids": ["att-pending"]})
            unknown = store.create("batch-unknown", "registered-batch",
                                   {"delivery_parent_id": "recipient-unknown"},
                                   {"attempt_ids": ["att-unknown"]})
            unknown = store.update(unknown["id"], state="unknown", observer_error="fixture-unknown")
            pending_before = store.get(pending["id"])
            unknown_before = store.get(unknown["id"])
            lock_path = store.root / "ensure-obligations.lock"
            original_inode = None

            for mode in ("normal", "error"):
                with self.subTest(callback_exit=mode):
                    def ensure_runner(**_kwargs):
                        nonlocal original_inode
                        inode = lock_path.stat().st_ino
                        if original_inode is None:
                            original_inode = inode
                        self.assertEqual(inode, original_inode)
                        self.assertNotEqual(lock_path.name,
                                            peer_steward.peer_obligations.RUNNER_LOCK_NAME)
                        matching_fds = []
                        for raw_fd in os.listdir("/proc/self/fd"):
                            try:
                                if os.readlink(f"/proc/self/fd/{raw_fd}") == str(lock_path):
                                    matching_fds.append(int(raw_fd))
                            except OSError:
                                continue
                        self.assertEqual(len(matching_fds), 1)
                        self.assertFalse(os.get_inheritable(matching_fds[0]))
                        self.assertIsNone(peer_steward._acquire_ensure_obligations_lock(store))

                        alternate = peer_steward.peer_obligations.ObligationStore(alternate_peer_root)
                        alternate_fd = peer_steward._acquire_ensure_obligations_lock(alternate)
                        self.assertIsNotNone(alternate_fd)
                        fcntl.flock(alternate_fd, fcntl.LOCK_UN)
                        os.close(alternate_fd)

                        child = subprocess.run(
                            [sys.executable, "-c",
                             "import os,sys; p=sys.argv[1]; print(any(os.readlink(f'/proc/self/fd/{fd}') == p "
                             "for fd in os.listdir('/proc/self/fd') if fd.isdecimal()))",
                             str(lock_path)],
                            close_fds=False, capture_output=True, text=True, check=False,
                        )
                        self.assertEqual(child.returncode, 0, child.stderr)
                        self.assertEqual(child.stdout.strip(), "False")
                        if mode == "error":
                            raise RuntimeError("fixture-runner-launch-error")
                        return True

                    with mock.patch.object(peer_steward.peer_obligations,
                                           "ensure_normal_runner", side_effect=ensure_runner) as runner, \
                         mock.patch.object(peer_steward, "_reconcile_external_obligations") as external, \
                         mock.patch.object(peer_steward, "_reconcile_obligations") as inline:
                        self.assertEqual(peer_steward.cmd_ensure_obligations(
                            SimpleNamespace(lifecycle_root=lifecycle_root)), 0)
                    self.assert_runner_request(runner, lifecycle_root)
                    external.assert_not_called()
                    inline.assert_not_called()

                    fd = peer_steward._acquire_ensure_obligations_lock(store)
                    self.assertIsNotNone(fd, "short lock stayed held after callback exit")
                    self.assertEqual(lock_path.stat().st_ino, original_inode)
                    fcntl.flock(fd, fcntl.LOCK_UN)
                    os.close(fd)
                    self.assertEqual(store.get(pending["id"]), pending_before)
                    self.assertEqual(store.get(unknown["id"]), unknown_before)

    def test_detached_lifecycle_initializes_only_beneath_an_existing_parent(self):
        parent = self.tmp_root / "lifecycle-parent"
        parent.mkdir()
        state_root = parent / "peer-state"
        with mock.patch.object(peer_steward.peer_obligations,
                               "peer_state_root", return_value=state_root), \
             mock.patch.object(peer_steward.peer_obligations,
                               "ensure_normal_runner") as runner:
            self.assertEqual(peer_steward.cmd_ensure_obligations(None), 0)

        self.assertEqual(state_root.stat().st_mode & 0o777, 0o700)
        self.assertTrue((state_root / "peer-steward" / "obligations"
                         / "ensure-obligations.lock").is_file())
        self.assert_runner_request(runner, None)
        store = runner.call_args.kwargs["_store"]
        self.assertEqual(store.state_root, state_root)
        self.assertFalse(store._allow_state_root_creation)

    def test_detached_lifecycle_does_not_recreate_a_missing_parent(self):
        state_root = self.tmp_root / "missing-parent" / "peer-state"
        with mock.patch.object(peer_steward.peer_obligations,
                               "peer_state_root", return_value=state_root), \
             mock.patch.object(peer_steward.peer_obligations,
                               "ensure_normal_runner") as runner:
            self.assertEqual(peer_steward.cmd_ensure_obligations(None), 0)
        runner.assert_not_called()
        self.assertFalse(state_root.parent.exists())

    def test_detached_lifecycle_rejects_root_replaced_before_recovery_lock(self):
        parent = self.tmp_root / "before-lock-parent"
        parent.mkdir()
        state_root = parent / "peer-state"
        moved_root = parent / "peer-state-held"
        original_acquire = peer_steward._acquire_ensure_obligations_lock

        def replace_before_lock(store, *, retain_directory=False):
            store.state_root.rename(moved_root)
            store.state_root.mkdir(mode=0o700)
            return original_acquire(store, retain_directory=retain_directory)

        with mock.patch.object(peer_steward.peer_obligations,
                               "peer_state_root", return_value=state_root), \
             patch_split(peer_steward, "_acquire_ensure_obligations_lock",
                               side_effect=replace_before_lock), \
             mock.patch.object(peer_steward.peer_obligations,
                               "ensure_normal_runner") as runner:
            self.assertEqual(peer_steward.cmd_ensure_obligations(None), 0)

        runner.assert_not_called()
        self.assertEqual(list(state_root.iterdir()), [])
        self.assertEqual(list(moved_root.iterdir()), [])

    def test_detached_lifecycle_rechecks_root_after_lock_before_runner_request(self):
        state_root = self.tmp_root / "after-lock-state"
        state_root.mkdir()
        moved_root = state_root.with_name("after-lock-state-held")
        original_ensure = self._original_ensure_normal_runner
        snapshots = {}

        def snapshot(path):
            return tuple((str(item.relative_to(path)), item.is_dir(),
                          None if item.is_dir() else item.read_bytes())
                         for item in sorted(path.rglob("*")))

        def replace_then_request(**kwargs):
            state_root.rename(moved_root)
            state_root.mkdir(mode=0o700)
            snapshots["held"] = snapshot(moved_root)
            snapshots["replacement"] = snapshot(state_root)
            return original_ensure(**kwargs)

        with mock.patch.object(peer_steward.peer_obligations,
                               "peer_state_root", return_value=state_root), \
             mock.patch.object(peer_steward.peer_obligations,
                               "ensure_normal_runner", side_effect=replace_then_request) as runner, \
             mock.patch.object(peer_steward.peer_obligations.subprocess,
                               "Popen") as spawn:
            self.assertEqual(peer_steward.cmd_ensure_obligations(None), 0)

        runner.assert_called_once()
        spawn.assert_not_called()
        self.assertEqual(snapshot(moved_root), snapshots["held"])
        self.assertEqual(snapshot(state_root), snapshots["replacement"])
        self.assertNotIn("runner-current.lock", {entry[0] for entry in snapshots["held"]})

    def test_detached_lifecycle_rejects_replaced_recovery_lock_inode(self):
        state_root = self.tmp_root / "lock-replacement-state"
        state_root.mkdir()
        original_ensure = self._original_ensure_normal_runner
        replaced = {}

        def replace_lock_then_request(**kwargs):
            lock_path = kwargs["_store"].root / "ensure-obligations.lock"
            held_path = lock_path.with_name("ensure-obligations.held")
            lock_path.rename(held_path)
            lock_path.touch()
            replaced["paths"] = (held_path, lock_path)
            replaced["snapshot"] = tuple((str(path.name), path.read_bytes())
                for path in sorted(kwargs["_store"].root.iterdir()) if path.is_file())
            return original_ensure(**kwargs)

        with mock.patch.object(peer_steward.peer_obligations,
                               "peer_state_root", return_value=state_root), \
             mock.patch.object(peer_steward.peer_obligations,
                               "ensure_normal_runner", side_effect=replace_lock_then_request) as runner, \
             mock.patch.object(peer_steward.peer_obligations.subprocess,
                               "Popen") as spawn:
            self.assertEqual(peer_steward.cmd_ensure_obligations(None), 0)

        runner.assert_called_once()
        spawn.assert_not_called()
        held_path, lock_path = replaced["paths"]
        self.assertEqual(tuple((str(path.name), path.read_bytes())
                         for path in sorted(lock_path.parent.iterdir()) if path.is_file()),
                         replaced["snapshot"])
        self.assertNotEqual(held_path.stat().st_ino, lock_path.stat().st_ino)

    def test_ensure_lock_is_released_when_its_process_is_killed(self):
        peer_root = self.tmp_root / "peer-killed-holder"
        peer_root.mkdir()
        marker = self.tmp_root / "ensure-entered"
        script = (
            "import importlib.util,os,sys,time\n"
            "from pathlib import Path\n"
            "spec=importlib.util.spec_from_file_location('peer_steward_test',%r)\n"
            "m=importlib.util.module_from_spec(spec); sys.modules[spec.name]=m; spec.loader.exec_module(m)\n"
            "def hold(**_kwargs):\n"
            "    Path(os.environ['ENSURE_MARKER']).write_text('entered')\n"
            "    time.sleep(30)\n"
            "m.peer_obligations.ensure_normal_runner=hold\n"
            "raise SystemExit(m.cmd_ensure_obligations(type('Args',(),{'lifecycle_root':None})()))\n"
        ) % str(_HERE / "peer-steward.py")
        env = dict(os.environ)
        env.update({"AGENT_PEER_LEDGER_ROOT": str(peer_root), "ENSURE_MARKER": str(marker)})
        holder = subprocess.Popen([sys.executable, "-c", script], env=env,
                                  stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL)
        try:
            deadline = time.monotonic() + 5
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(marker.exists(), "ensure callback did not acquire its short lock")
            store = peer_steward.peer_obligations.ObligationStore(peer_root)
            lock_path = store.root / "ensure-obligations.lock"
            inode = lock_path.stat().st_ino
            self.assertIsNone(peer_steward._acquire_ensure_obligations_lock(store))
            holder.kill()
            self.assertEqual(holder.wait(timeout=3), -signal.SIGKILL)
            fd = peer_steward._acquire_ensure_obligations_lock(store)
            self.assertIsNotNone(fd, "SIGKILL left the process lock held")
            self.assertEqual(lock_path.stat().st_ino, inode)
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
        finally:
            if holder.poll() is None:
                holder.kill()
                holder.wait(timeout=3)

    def test_existing_lifecycle_callback_reconnects_through_one_runner(self):
        self.pending_message()
        with mock.patch.object(peer_steward, "_watch_reconciliation_needed", return_value=False), \
             mock.patch.object(peer_steward.dispatch_batch_obligations,
                               "has_unowned_work", return_value=False), \
             mock.patch.object(peer_steward.peer_obligations, "ensure_normal_runner", return_value=True) as runner, \
             mock.patch.object(peer_steward, "_reconcile_obligations") as inline, \
             mock.patch.object(peer_steward, "_ensure_watch_observers") as watches, \
             mock.patch.object(peer_steward.dispatch_batch_obligations,
                               "ensure_observers") as batches:
            self.assertEqual(peer_steward.main(["__ensure-obligations"]), 0)
        self.assert_runner_request(runner, None)
        inline.assert_not_called()
        watches.assert_not_called()
        batches.assert_not_called()

    def test_unavailable_runner_retains_work_without_blocking_the_callback(self):
        store, duty = self.pending_message()
        with mock.patch.object(peer_steward, "_reconcile_external_obligations"), \
             mock.patch.object(peer_steward.peer_obligations, "ensure_normal_runner", return_value=False) as runner, \
             mock.patch.object(peer_steward, "_reconcile_obligations") as inline:
            self.assertEqual(peer_steward.cmd_ensure_obligations(None), 0)
        self.assert_runner_request(runner, None)
        inline.assert_not_called()
        self.assertEqual(store.get(duty["id"])["state"], "pending")

    def test_live_peer_runner_does_not_hide_new_external_work_on_next_callback(self):
        self.pending_message()
        with mock.patch.object(peer_steward, "_watch_reconciliation_needed",
                               side_effect=[False, True]), \
             mock.patch.object(peer_steward.dispatch_batch_obligations,
                               "has_unowned_work", side_effect=[False, True]), \
             mock.patch.object(peer_steward.peer_obligations, "ensure_normal_runner", return_value=True) as runner, \
             mock.patch.object(peer_steward, "_ensure_watch_observers") as watches, \
             mock.patch.object(peer_steward.dispatch_batch_obligations,
                               "ensure_observers") as batches, \
             mock.patch.object(peer_steward, "_reconcile_obligations") as peer_work:
            self.assertEqual(peer_steward.cmd_ensure_obligations(None), 0)
            watches.assert_not_called()
            batches.assert_not_called()
            self.assertEqual(peer_steward.cmd_ensure_obligations(None), 0)
        watches.assert_not_called()
        batches.assert_not_called()
        self.assertEqual(runner.call_count, 2)
        peer_work.assert_not_called()

    def test_external_only_callback_recovers_without_an_idle_peer_runner(self):
        with mock.patch.object(peer_steward, "_watch_reconciliation_needed", return_value=True), \
             mock.patch.object(peer_steward.dispatch_batch_obligations,
                               "has_unowned_work", return_value=True), \
             mock.patch.object(peer_steward, "_ensure_watch_observers") as watches, \
             mock.patch.object(peer_steward.dispatch_batch_obligations,
                               "ensure_observers") as batches, \
             mock.patch.object(peer_steward.peer_obligations, "ensure_normal_runner") as runner:
            self.assertEqual(peer_steward.cmd_ensure_obligations(None), 0)
        watches.assert_not_called()
        batches.assert_not_called()
        self.assert_runner_request(runner, None)

    def test_managed_lifecycle_keeps_the_existing_runner_for_external_duties(self):
        lifecycle_root = self.tmp_root / "managed-release"
        signature = (("watch-fixture",),), ()
        with mock.patch.object(peer_steward.peer_obligations,
                               "managed_service_binding", return_value="/fixture/current"), \
             mock.patch.object(peer_steward, "_reconcile_external_obligations"), \
             mock.patch.object(peer_steward, "_external_obligation_signature",
                               return_value=signature), \
             mock.patch.object(peer_steward, "_runner_duties", return_value=[]), \
             mock.patch.object(peer_steward.peer_obligations,
                               "ensure_normal_runner", return_value=True) as ensure:
            self.assertEqual(peer_steward.cmd_ensure_obligations(
                SimpleNamespace(lifecycle_root=lifecycle_root)), 0)
        self.assert_runner_request(ensure, lifecycle_root)

    def test_managed_runner_bounds_external_idle_scans_and_resets_for_new_batch(self):
        import dispatch_batch_obligations as batch

        def create_batch(suffix):
            args = SimpleNamespace(
                jobs=self.jobs_path, sealed_batch_id="sealed-" + suffix,
                parent_attempt_id="att-parent", parent_session_id=None,
                attempt_id=["att-child-" + suffix], control_socket=None,
                queue_socket=None, thread_id=None, interval=1.0, timeout=2.0,
            )
            duty_id, _ = batch.create(args)
            batch.update(self.jobs_path, duty_id, state="delivery-pending",
                         reason="awaiting-recipient", outcome={"state": "done"})
            return duty_id

        first = create_batch("first")
        watch_root = peer_steward._watch_root()
        watch_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        watch_id = "watch-idle-fixture"
        watch_paths = peer_steward._watch_paths(watch_id, watch_root)
        peer_steward._write_json_atomic(watch_paths.arm, {
            "schema_version": peer_steward._WATCH_SCHEMA, "watch_id": watch_id,
            "target": "fixture-target", "agent": {"harness": "codex",
                "session_id": "sid-fixture", "pane": "w1:p1"},
            "watcher": {"pid": 123, "pid_start": "456"},
        })

        store = peer_steward.peer_obligations.ObligationStore()
        lock_path = store.root / peer_steward.peer_obligations.RUNNER_LOCK_NAME
        lock_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        delays = []
        second = []

        def observed_delay(delay):
            delays.append(delay)
            if len(delays) == 2:
                second.append(create_batch("resumed"))
            elif len(delays) == 3:
                batch.update(self.jobs_path, first, state="complete", reason="fixture-settled")
                batch.update(self.jobs_path, second[0], state="complete", reason="fixture-settled")
                peer_steward._write_json_atomic(watch_paths.receipt, {"watch_id": watch_id})

        try:
            with mock.patch.object(peer_steward, "_reconcile_obligations"), \
                 mock.patch.object(peer_steward, "_reconcile_external_obligations") as reconcile, \
                 mock.patch.object(batch, "_jobs_paths", return_value=[self.jobs_path.resolve()]), \
                 mock.patch.object(peer_steward.peer_obligations, "exec_service_runner"), \
                 mock.patch.object(peer_steward.time, "sleep", side_effect=observed_delay):
                self.assertEqual(peer_steward.cmd_obligation_runner(SimpleNamespace(
                    state_root=self.tmp_root, lock_fd=lock_fd,
                    service_current="/fixture/current")), 0)
        finally:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            except OSError:
                pass
            os.close(lock_fd)
        self.assertEqual(delays, [1.0, 1.7, 1.0])
        self.assertEqual(reconcile.call_count, 4)
        self.assertEqual(len(second), 1, "a batch arriving during idle resumes the bounded scan")

    def test_fixed_root_runner_reconnects_external_families_once_per_pass(self):
        store, duty = self.pending_message()
        ticks = []

        def resume(row, current, *, read_context=None):
            ticks.append(row["id"])
            if len(ticks) == 3:
                current.update(row["id"], state="complete")

        with (store.root / peer_steward.peer_obligations.RUNNER_LOCK_NAME).open("w") as lock, \
             mock.patch.object(peer_steward, "_resume_message_obligation", side_effect=resume), \
             mock.patch.object(peer_steward, "_ensure_watch_observers") as watches, \
             mock.patch.object(peer_steward, "_watch_reconciliation_needed") as watch_probe, \
             mock.patch.object(peer_steward.dispatch_batch_obligations,
                               "ensure_observers") as batches, \
             mock.patch.object(peer_steward.dispatch_batch_obligations,
                               "has_unowned_work") as batch_probe, \
             mock.patch.object(peer_steward.time, "sleep") as sleep:
            fcntl.flock(lock, fcntl.LOCK_EX)
            self.assertEqual(peer_steward.cmd_obligation_runner(
                SimpleNamespace(state_root=self.tmp_root, lock_fd=lock.fileno())), 0)
        self.assertEqual(ticks, [duty["id"]] * 3)
        self.assertEqual(sleep.call_count, 2)
        for operation in (watches, watch_probe, batches):
            self.assertEqual(operation.call_count, 3)
        batch_probe.assert_not_called()

    def test_runner_closes_settlement_watcher_when_its_context_retires(self):
        store = peer_steward.peer_obligations.ObligationStore(self.tmp_root)
        lock_path = store.root / peer_steward.peer_obligations.RUNNER_LOCK_NAME
        contexts = []
        context_type = peer_steward.dispatch_batch_obligations.OwnerSettlementReadContext

        def make_context():
            context = context_type()
            if not context._ensure_dependency_watcher():
                self.skipTest("inotify is unavailable")
            contexts.append((context, context._dependency_watch_fd))
            return context

        with lock_path.open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            with mock.patch.object(
                    peer_steward.dispatch_batch_obligations,
                    "OwnerSettlementReadContext", side_effect=make_context), \
                 mock.patch.object(peer_steward, "_runner_readiness_loop"), \
                 patch_split(peer_steward, "_external_obligation_snapshot",
                                   return_value={"jobs_stamps": {}}), \
                 mock.patch.object(peer_steward, "_external_obligation_signature",
                                   return_value=((), ())), \
                 mock.patch.object(peer_steward, "_external_obligation_snapshot_unchanged",
                                   return_value=True), \
                 mock.patch.object(peer_steward, "_reconcile_external_obligations"), \
                 mock.patch.object(peer_steward, "_publish_watch_readiness"):
                self.assertEqual(peer_steward.cmd_obligation_runner(SimpleNamespace(
                    state_root=self.tmp_root, lock_fd=lock.fileno())), 0)

        self.assertEqual(len(contexts), 1)
        context, watcher_fd = contexts[0]
        self.assertEqual(context._dependency_watch_fd, -1)
        self.assertTrue(context._dependency_watcher_fallback)
        with self.assertRaises(OSError):
            os.fstat(watcher_fd)

    def run_three_service_passes(self, exec_service, *, on_external=None):
        """Run a service-mode runner for three passes and record its settlement contexts.

        ``on_external(settlement_context)`` runs where the runner would settle owners."""
        store, duty = self.pending_message()
        ticks = []
        record = SimpleNamespace(created=[], closed=[], external=[], exec_kwargs=[],
                                 readiness_threads=[], exit=None)
        context_type = peer_steward.dispatch_batch_obligations.OwnerSettlementReadContext
        real_close = context_type.close

        def resume(row, current, *, read_context=None):
            ticks.append(row["id"])
            if len(ticks) == 3:
                current.update(row["id"], state="complete")

        def make_context():
            context = context_type()
            record.created.append(context)
            return context

        def close(context):
            record.closed.append(context)
            return real_close(context)

        def readiness(_state_root, _lock_fd, _incarnation, stop):
            record.readiness_threads.append(threading.current_thread())
            stop.wait()

        def exec_runner(*args, **kwargs):
            record.exec_kwargs.append(kwargs)
            return exec_service(record, kwargs)

        def reconcile_external(*_args, settlement_context=None, **_kwargs):
            record.external.append(settlement_context)
            if on_external is not None:
                on_external(settlement_context)

        with (store.root / peer_steward.peer_obligations.RUNNER_LOCK_NAME).open("w") as lock, \
             mock.patch.object(peer_steward, "_resume_message_obligation", side_effect=resume), \
             mock.patch.object(peer_steward.dispatch_batch_obligations,
                               "OwnerSettlementReadContext", side_effect=make_context), \
             mock.patch.object(context_type, "close", autospec=True, side_effect=close), \
             mock.patch.object(peer_steward, "_runner_readiness_loop", side_effect=readiness), \
             mock.patch.object(peer_steward.peer_obligations, "exec_service_runner",
                               side_effect=exec_runner), \
             patch_split(peer_steward, "_external_obligation_snapshot",
                         return_value={"jobs_stamps": {}}), \
             mock.patch.object(peer_steward, "_external_obligation_signature",
                               return_value=((), ())), \
             mock.patch.object(peer_steward, "_external_obligation_snapshot_unchanged",
                               return_value=True), \
             mock.patch.object(peer_steward, "_reconcile_external_obligations",
                               side_effect=reconcile_external), \
             mock.patch.object(peer_steward, "_publish_watch_readiness"), \
             mock.patch.object(peer_steward.time, "sleep"):
            fcntl.flock(lock, fcntl.LOCK_EX)
            record.exit = peer_steward.cmd_obligation_runner(SimpleNamespace(
                state_root=self.tmp_root, lock_fd=lock.fileno(),
                service_current="/fixture/current"))
        self.assertFalse(any(thread.is_alive() for thread in record.readiness_threads),
                         "the runner must not leave a readiness thread behind")
        return record

    def test_service_runner_keeps_one_settlement_context_across_passes_without_replacement(self):
        record = self.run_three_service_passes(lambda _record, _kwargs: False)

        self.assertEqual(record.exit, 0)
        self.assertEqual(len(record.created), 1,
                         "a service pass without replacement must keep its settlement context")
        self.assertEqual(len(record.external), 3)
        self.assertTrue(all(context is record.created[0] for context in record.external))
        self.assertEqual(len(record.exec_kwargs), 3,
                         "every service pass still asks for a replacement")
        self.assertTrue(all(callable(kwargs.get("before_exec")) for kwargs in record.exec_kwargs))
        self.assertEqual(len(record.readiness_threads), 1)
        self.assertEqual(record.closed, [record.created[0]])

    def test_service_runner_retires_context_and_readiness_only_at_replacement_exec(self):
        class ReplacedByExec(Exception):
            pass

        at_exec = {}

        def exec_service(record, kwargs):
            if len(record.exec_kwargs) == 1:
                self.assertEqual(record.closed, [],
                                 "a pass that only asks for a replacement must not retire its context")
                return False
            kwargs["before_exec"]()
            at_exec["closed"] = list(record.closed)
            at_exec["readiness_alive"] = any(thread.is_alive() for thread in record.readiness_threads)
            raise ReplacedByExec()

        with self.assertRaises(ReplacedByExec):
            self.run_three_service_passes(exec_service)

        self.assertFalse(at_exec["readiness_alive"],
                         "the readiness worker must be joined before the process image is replaced")
        self.assertEqual(len(at_exec["closed"]), 1)

    def test_service_runner_restores_context_after_failed_replacement_exec(self):
        def exec_service(record, kwargs):
            if len(record.exec_kwargs) == 2:
                kwargs["before_exec"]()
                self.assertEqual(len(record.closed), 1)
            return False

        record = self.run_three_service_passes(exec_service)

        self.assertEqual(record.exit, 0)
        self.assertEqual(len(record.created), 2)
        retired, restored = record.created
        self.assertEqual(record.external, [retired, restored, restored],
                         "the retired context must never serve a later settlement")
        self.assertEqual(record.closed, [retired, restored])
        self.assertEqual(len(record.readiness_threads), 2)

    def test_service_runner_serves_every_settled_owner_from_the_kept_context_on_later_passes(self):
        """A kept settlement context answers each finished (``complete``) owner from its cache.

        The runner settles the same owner rows on every pass. A context rebuilt each pass proves
        every one of them again from scratch; the kept one serves all of them as cache hits when
        the registry is read again in full (the file was replaced, so its owner cursor is void),
        and proves none again while the registry stays as it was."""
        import dispatch_registry_cache
        from dispatch_terminal_commit import CompletionState
        batch = peer_steward.dispatch_batch_obligations
        owners = ["att-owner-%d" % index for index in range(5)]
        jobs = self.tmp_root / "owner-jobs.log"
        text = "".join(
            "2026-10-18T00:00:00Z\tdone\t/repo\t/wt\towner\t" + ",".join((
                "attempt_schema_version=2", "dispatch_depth=1", "worker_type=owner",
                "launch_started=1", "supervisor_lease=flock-v1",
                "supervisor_lease_file=/fixture/owner.lease", "log_file=/fixture/owner.log",
                "attempt_id=%s" % attempt, "workflow_completion=runtime-v1",
                "note=completed-supervisor")) + "\n" for attempt in owners)
        jobs.write_text(text, encoding="utf-8")
        evidence = self.tmp_root / "complete-evidence.json"
        evidence.write_text("sealed", encoding="utf-8")
        dispatch_registry_cache.clear_cache()
        seen = []

        def settle_owners(context):
            if len(seen) == 1:
                # Same rows in a replaced file: the next read is a full read.
                replacement = jobs.with_name("owner-jobs.replacement")
                replacement.write_text(text, encoding="utf-8")
                os.replace(replacement, jobs)
            batch._resume_owner_settlements(jobs, context=context)
            seen.append((context, context.metrics()))

        with mock.patch.object(batch, "_owner_settlement_dependency_paths", return_value=(evidence,)), \
             mock.patch("dispatch_terminal_commit.owner_completion_state",
                        return_value=CompletionState("complete")), \
             split_patch("dispatch_contract.supervisor_lease_is_held", return_value=False), \
             mock.patch("dispatch_completion_join.materialize_after_terminal_close") as materialize:
            record = self.run_three_service_passes(
                lambda _record, _kwargs: False, on_external=settle_owners)

        self.assertEqual(record.exit, 0)
        self.assertEqual(len(seen), 3)
        first_context, first = seen[0]
        self.assertEqual((first["historical_checks"], first["settled_cache_hits"]), (len(owners), 0),
                         "the first pass proves each finished owner once")
        self.assertEqual(
            seen[1][1]["settled_cache_hits"], len(owners),
            "every complete owner row is a cache hit when the next pass reads the registry in full")
        self.assertEqual(seen[2][1]["settled_cache_hits"], len(owners),
                         "an unchanged registry keeps the owner cursor: nothing is looked at again")
        for later_context, metrics in seen[1:]:
            self.assertIs(later_context, first_context)
            self.assertEqual(metrics["historical_checks"], len(owners),
                             "a later pass must not prove a finished owner again")
        materialize.assert_not_called()

    def test_runner_idle_backoff_ignores_repeated_observation_writes_and_resets_on_change(self):
        store, duty = self.pending_message()
        ticks = []

        def resume(row, current, *, read_context=None):
            ticks.append(row["id"])
            if len(ticks) <= 2:
                current.update(row["id"], observer_error="same-unavailable")
            elif len(ticks) == 3:
                current.update(row["id"], observation={"reason": "recipient-returned"})
            else:
                current.update(row["id"], state="complete")

        clock = {"now": 0.0}

        def advance_sleep(delay):
            clock["now"] += delay

        with (store.root / peer_steward.peer_obligations.RUNNER_LOCK_NAME).open("w") as lock, \
             mock.patch.object(peer_steward, "_resume_message_obligation", side_effect=resume), \
             mock.patch.object(peer_steward.time, "monotonic", side_effect=lambda: clock["now"]), \
             mock.patch.object(peer_steward.time, "sleep", side_effect=advance_sleep) as sleep:
            fcntl.flock(lock, fcntl.LOCK_EX)
            self.assertEqual(peer_steward.cmd_obligation_runner(
                SimpleNamespace(state_root=self.tmp_root, lock_fd=lock.fileno())), 0)
        self.assertEqual(ticks, [duty["id"]] * 4)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [1.0, 1.7, 1.0])

    def test_runner_ends_when_its_state_root_is_removed_with_duties_still_pending(self):
        # A test's temporary home removed under a runner left it spinning on duties
        # nobody could reach; the next pass after removal now ends it.
        store, duty = self.pending_message()
        ticks = []
        lock_path = store.root / peer_steward.peer_obligations.RUNNER_LOCK_NAME

        def resume(row, current, *, read_context=None):
            ticks.append(row["id"])
            current.update(row["id"], observer_error="recipient-unavailable")

        def remove_root(_delay):
            if not store.root.exists():
                raise AssertionError("runner kept going after its state root was removed")
            shutil.rmtree(store.root)

        with lock_path.open("w") as lock, \
             mock.patch.object(peer_steward, "_resume_message_obligation", side_effect=resume), \
             mock.patch.object(peer_steward.time, "sleep", side_effect=remove_root) as sleep:
            fcntl.flock(lock, fcntl.LOCK_EX)
            self.assertEqual(peer_steward.cmd_obligation_runner(
                SimpleNamespace(state_root=self.tmp_root, lock_fd=lock.fileno())), 0)
        self.assertEqual(ticks, [duty["id"]])
        self.assertEqual(sleep.call_count, 1)

    def test_runner_ends_when_its_lock_file_is_replaced(self):
        store, duty = self.pending_message()
        lock_path = store.root / peer_steward.peer_obligations.RUNNER_LOCK_NAME

        def replace_lock(_delay):
            if sleep.call_count > 1:
                raise AssertionError("runner kept going after its lock file was replaced")
            lock_path.unlink()
            lock_path.write_text("")

        with lock_path.open("w") as lock, \
             mock.patch.object(peer_steward, "_resume_message_obligation"), \
             mock.patch.object(peer_steward.time, "sleep", side_effect=replace_lock) as sleep:
            fcntl.flock(lock, fcntl.LOCK_EX)
            self.assertEqual(peer_steward.cmd_obligation_runner(
                SimpleNamespace(state_root=self.tmp_root, lock_fd=lock.fileno())), 0)
        self.assertEqual(sleep.call_count, 1)

    def test_work_accepted_at_unlock_is_reclaimed_before_runner_exit(self):
        store = peer_steward.peer_obligations.ObligationStore()
        store.root.mkdir(parents=True, exist_ok=True)
        accepted = []
        real_flock = fcntl.flock

        def unlock_and_accept(fd, operation):
            result = real_flock(fd, operation)
            if operation == fcntl.LOCK_UN and not accepted:
                accepted.append(store.create("message-at-exit", "message",
                    {"harness": "codex", "session_id": "sid-exit"}, {}))
            return result

        def resume(row, current, *, read_context=None):
            current.update(row["id"], state="complete")

        with (store.root / peer_steward.peer_obligations.RUNNER_LOCK_NAME).open("w") as lock, \
             mock.patch.object(peer_steward, "_resume_message_obligation",
                               side_effect=resume) as resumed:
            real_flock(lock, fcntl.LOCK_EX)
            with mock.patch.object(peer_steward.fcntl, "flock", side_effect=unlock_and_accept):
                self.assertEqual(peer_steward.cmd_obligation_runner(
                    SimpleNamespace(state_root=self.tmp_root, lock_fd=lock.fileno())), 0)
        self.assertEqual(len(accepted), 1)
        self.assertEqual(resumed.call_count, 1)
        self.assertEqual(store.get(accepted[0]["id"])["state"], "complete")

    def test_external_batch_created_at_unlock_invalidates_the_runner_snapshot(self):
        import dispatch_batch_obligations as batch

        store = peer_steward.peer_obligations.ObligationStore()
        store.root.mkdir(parents=True, exist_ok=True)
        lock_path = store.root / peer_steward.peer_obligations.RUNNER_LOCK_NAME
        lock = lock_path.open("w")
        watch_root = self.tmp_root / "stable-watch-root"
        watch_root.mkdir()
        real_flock = fcntl.flock
        created = []
        inside_create = False
        delays = []

        def unlock_and_accept(fd, operation):
            nonlocal inside_create
            result = real_flock(fd, operation)
            if operation == fcntl.LOCK_UN and not inside_create and not created:
                inside_create = True
                args = SimpleNamespace(
                    jobs=self.jobs_path, sealed_batch_id="accepted-at-unlock",
                    parent_attempt_id="att-parent", parent_session_id=None,
                    attempt_id=["att-child-unlock"], control_socket=None,
                    queue_socket=None, thread_id=None, interval=1.0, timeout=2.0,
                )
                created.append(batch.create(args)[0])
                inside_create = False
            return result

        def settle_before_next_pass(delay):
            delays.append(delay)
            batch.update(self.jobs_path, created[0], state="complete",
                         reason="fixture-settled")

        original_snapshot = peer_steward._external_obligation_snapshot
        snapshots = []

        def count_snapshot():
            value = original_snapshot()
            snapshots.append(value)
            return value

        try:
            real_flock(lock.fileno(), fcntl.LOCK_EX)
            with patch_split(peer_steward, "_watch_root", return_value=watch_root), \
                 mock.patch.object(batch, "_jobs_paths", side_effect=lambda jobs=None, strict=True: [
                    Path(jobs).resolve(strict=False) if jobs else self.jobs_path.resolve()
                 ]), \
                 patch_split(peer_steward, "_external_obligation_snapshot",
                                   side_effect=count_snapshot), \
                 mock.patch.object(peer_steward, "_reconcile_external_obligations"), \
                 mock.patch.object(peer_steward.peer_obligations,
                                   "exec_service_runner", return_value=False), \
                 mock.patch.object(peer_steward.time, "sleep", side_effect=settle_before_next_pass), \
                 mock.patch.object(peer_steward.fcntl, "flock", side_effect=unlock_and_accept):
                self.assertEqual(peer_steward.cmd_obligation_runner(SimpleNamespace(
                    state_root=self.tmp_root, lock_fd=lock.fileno(),
                    service_current="/fixture/current")), 0)
        finally:
            try:
                real_flock(lock.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
            lock.close()

        self.assertEqual(len(created), 1)
        self.assertEqual(delays, [1.0])
        self.assertEqual(len(snapshots), 3,
                         "the new batch is reread after unlock and the completed row on the next pass")
        self.assertEqual(batch.read(self.jobs_path, created[0])["state"], "complete")

    def test_watch_snapshot_brackets_scan_and_reuses_stable_pair(self):
        import dispatch_batch_obligations as batch

        root = self.tmp_root / "watch-snapshot-race"
        root.mkdir()
        batch_root = batch._root(self.jobs_path)
        batch_root.mkdir(parents=True)
        with patch_split(peer_steward, "_watch_root", return_value=root), \
             mock.patch.object(batch, "_jobs_paths", return_value=[self.jobs_path.resolve()]):
            stable = peer_steward._external_obligation_snapshot()
            self.assertTrue(stable["watch_cacheable"])
            self.assertTrue(stable["batches"]["cacheable"])
            self.assertTrue(peer_steward._external_obligation_snapshot_unchanged(stable))

            original = peer_steward._watch_entries
            created = []

            def scan_then_create(scan_root):
                entries = original(scan_root)
                if not created:
                    watch_id = "watch-created-inside-scan"
                    peer_steward._write_json_atomic(
                        peer_steward._watch_paths(watch_id, root).arm,
                        {"schema_version": peer_steward._WATCH_SCHEMA,
                         "watch_id": watch_id, "target": "synthetic-target",
                         "agent": {"harness": "codex", "session_id": "synthetic-session",
                                   "pane": "w1:p1"},
                         "watcher": {"pid": 1, "pid_start": "1"}},
                    )
                    created.append(watch_id)
                return entries

            with patch_split(peer_steward, "_watch_entries", side_effect=scan_then_create):
                stale = peer_steward._external_obligation_snapshot()
                self.assertFalse(stale["watch_cacheable"])
                self.assertEqual(stale["watches"], ())
                self.assertFalse(peer_steward._external_obligation_snapshot_unchanged(stale))
                refreshed = peer_steward._external_obligation_snapshot()

        self.assertIn(created[0], [watch_id for watch_id, _arm in refreshed["watches"]])

    def test_batch_created_between_scan_and_stamp_is_reclaimed_by_runner(self):
        import dispatch_batch_obligations as batch

        store = peer_steward.peer_obligations.ObligationStore()
        store.root.mkdir(parents=True, exist_ok=True)
        lock = (store.root / peer_steward.peer_obligations.RUNNER_LOCK_NAME).open("w")
        fcntl.flock(lock, fcntl.LOCK_EX)
        watch_root = self.tmp_root / "empty-watch-root"
        watch_root.mkdir()
        batch_root = batch._root(self.jobs_path)
        batch_root.mkdir(parents=True)
        args = SimpleNamespace(
            jobs=self.jobs_path, sealed_batch_id="accepted-during-read",
            parent_attempt_id="att-parent", parent_session_id=None,
            attempt_id=["att-child-scan-race"], delivery_parent_id="att-parent",
            control_socket=None, queue_socket=None, thread_id=None,
            interval=1.0, timeout=2.0,
        )
        created = []
        watch_created = []
        producer_results = []
        snapshots = []
        launches = []
        original_watch_entries = peer_steward._watch_entries
        original_records = batch._read_only_records
        original_snapshot = peer_steward._external_obligation_snapshot

        def scan_watch_then_accept(root):
            entries = original_watch_entries(root)
            if not watch_created:
                watch_id = "watch-accepted-during-read"
                peer_steward._write_json_atomic(
                    peer_steward._watch_paths(watch_id, watch_root).arm,
                    {"schema_version": peer_steward._WATCH_SCHEMA,
                     "watch_id": watch_id, "target": "synthetic-watch-target",
                     "agent": {"harness": "codex", "session_id": "synthetic-watch-session",
                               "pane": "w1:p1"},
                     "watcher": {}},
                )
                watch_created.append(watch_id)
            return entries

        def scan_then_accept(jobs):
            records = original_records(jobs)
            if not created:
                duty_id, _record = batch.create(args)
                batch.update(jobs, duty_id, state="delivery-pending",
                             outcome={"state": "settled", "attempts": args.attempt_id})
                created.append(duty_id)
                producer_results.append(peer_steward.peer_obligations._ensure_runner(
                    store.root.parent.parent, use_remembered=True,
                    reconciliation_required=True))
            return records

        def count_snapshot():
            snapshot = original_snapshot()
            snapshots.append(snapshot)
            return snapshot

        class Launched:
            pid = 901234

        def launch(command, **kwargs):
            # The inbox prefill observes the pane census through `subprocess.run`
            # (which calls Popen); that is a read, not an obligation launch.
            if command and Path(str(command[0])).name == "herdr":
                return Launched()
            if "--resume-obligation" in command:
                launches.append(("batch", command, batch.read(self.jobs_path, created[0]), kwargs))
            else:
                launches.append(("watch", command, None, kwargs))
            return Launched()

        sleeps = []

        def settle_after_reclaim(_delay):
            sleeps.append(_delay)
            if len(sleeps) == 2:
                batch.update(self.jobs_path, created[0], state="complete",
                             reason="synthetic-owner-progress")
                peer_steward._write_json_atomic(
                    peer_steward._watch_paths(watch_created[0], watch_root).receipt,
                    {"watch_id": watch_created[0], "state": "synthetic-complete"})

        try:
            with patch_split(peer_steward, "_watch_root", return_value=watch_root), \
                 patch_split(peer_steward, "_watch_entries",
                                   side_effect=scan_watch_then_accept), \
                 mock.patch.object(batch, "_jobs_paths", return_value=[self.jobs_path.resolve()]), \
                 mock.patch.object(batch, "_read_only_records", side_effect=scan_then_accept), \
                 patch_split(peer_steward, "_external_obligation_snapshot",
                                   side_effect=count_snapshot), \
                 mock.patch.object(peer_steward.peer_obligations, "exec_service_runner",
                                   return_value=False), \
                 mock.patch.object(peer_steward, "_settle_terminal_watch_if_unobserved",
                                   return_value=False), \
                 patch_split(peer_steward, "_herdr_missing", return_value=False), \
                 patch_split(peer_steward, "_watch_terminal_from_view", return_value=None), \
                 mock.patch.object(peer_steward.herdr_observation,
                                   "socket_path_for_endpoint", return_value=None), \
                 mock.patch.object(batch.subprocess, "Popen", side_effect=launch), \
                 mock.patch.object(peer_steward.time, "sleep", side_effect=settle_after_reclaim):
                self.assertEqual(peer_steward.cmd_obligation_runner(SimpleNamespace(
                    state_root=self.tmp_root, lock_fd=lock.fileno(),
                    service_current="/fixture/current")), 0)
        finally:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
            lock.close()

        self.assertEqual(producer_results, [False],
                         "the producer saw the held runner lock and left responsibility with it")
        self.assertEqual(len(launches), 2,
                         "the real runner must reclaim both accepted external obligations")
        self.assertFalse(snapshots[0]["watch_cacheable"])
        self.assertEqual(snapshots[0]["watches"], ())
        self.assertFalse(snapshots[0]["batches"]["cacheable"])
        self.assertEqual(snapshots[0]["batches"]["signature"], ())
        self.assertTrue(any(created[0] in [item[1] for item in snapshot["batches"]["signature"]]
                            for snapshot in snapshots[1:]))
        self.assertTrue(any(watch_created[0] in [watch_id for watch_id, _arm in snapshot["watches"]]
                            for snapshot in snapshots[1:]))
        self.assertEqual([kind for kind, _command, _record, _kwargs in launches], ["watch", "batch"])
        _kind, command, accepted, _kwargs = launches[1]
        self.assertIn("--resume-obligation", command)
        self.assertIn(created[0], command)
        self.assertEqual(accepted["state"], "delivery-pending")
        recovered = batch.read(self.jobs_path, created[0])
        self.assertEqual(recovered["state"], "complete")
        self.assertEqual(recovered["reason"], "synthetic-owner-progress")
        self.assertEqual(recovered["observer"]["pid"], Launched.pid)
        self.assertTrue(peer_steward._watch_paths(watch_created[0], watch_root).receipt.is_file())

    def test_scan_then_stamp_legacy_order_is_caught_at_real_runner_boundary(self):
        import dispatch_batch_obligations as batch

        def legacy_batch_snapshot(jobs=None):
            signature, paths_by_jobs, roots_by_jobs, root_stamps = [], {}, {}, {}
            cacheable = True
            for jobs_path in batch._jobs_paths(jobs, strict=False):
                key = str(jobs_path.resolve(strict=False))
                root = jobs_path.resolve(strict=False).parent / "supervisor-state" / "obligations"
                roots_by_jobs[key] = root
                try:
                    records = batch._read_only_records(jobs_path)
                except (OSError, batch.BatchObligationError):
                    signature.append((str(jobs_path), "unavailable"))
                    paths_by_jobs[key] = None
                    root_stamps[key] = batch._directory_stamp(root)
                    cacheable = False
                    continue
                root_stamps[key] = batch._directory_stamp(root)
                pending_paths = []
                for path, record, error in records:
                    if error is not None:
                        signature.append((str(jobs_path), path.name, "unreadable"))
                        pending_paths.append(path)
                    elif record and record.get("state") in batch._PENDING:
                        pending_paths.append(path)
                        observer = record.get("observer") or {}
                        signature.append((
                            str(jobs_path), record.get("id", path.stem), record.get("state"),
                            record.get("delivery"), observer.get("pid"), observer.get("pid_start"),
                        ))
                paths_by_jobs[key] = tuple(pending_paths)
            return {"signature": tuple(sorted(signature)), "paths_by_jobs": paths_by_jobs,
                    "roots_by_jobs": roots_by_jobs, "root_stamps": root_stamps,
                    "cacheable": cacheable}

        def legacy_external_snapshot():
            try:
                root = peer_steward._watch_root()
                watches = tuple(peer_steward._watch_entries(root))
                stamp = batch._directory_stamp(root)
                cacheable = True
            except Exception:
                root, watches, stamp, cacheable = None, None, None, False
            try:
                batches = batch.pending_observer_snapshot()
            except Exception:
                batches = {"signature": (("batch-state-unavailable",),),
                           "paths_by_jobs": {}}
            return {"watch_root": root, "watches": watches, "watch_stamp": stamp,
                    "watch_cacheable": cacheable, "batches": batches}

        result = unittest.TestResult()
        regression = ObligationStartupTest(
            "test_batch_created_between_scan_and_stamp_is_reclaimed_by_runner")
        with mock.patch.object(batch, "pending_observer_snapshot",
                               side_effect=legacy_batch_snapshot), \
             patch_split(peer_steward, "_external_obligation_snapshot",
                               side_effect=legacy_external_snapshot):
            regression.run(result)

        # The focused boundary regression must fail with the old scan→stamp
        # order; the current-source test in this class passes and proves both
        # obligations were reclaimed by the coordinator.
        self.assertEqual(result.errors, [])
        self.assertEqual(len(result.failures), 1)
        self.assertRegex(result.failures[0][1], "0 != 2|True is not false")


class SidlessObligationProcessBindingTest(_TmpRootMixin, unittest.TestCase):
    @staticmethod
    def _pane_info():
        return {"pane_id": "w1:pX", "shell_pid": os.getpid(),
                "foreground_process_group_id": os.getpid(),
                "foreground_processes": [{"pid": 4242, "argv": ["codex"]}]}

    def _row_and_duty(self, *, process_proof=True, submitted=False):
        sender = {"harness": "claude", "session_id": "sid-sender", "name": "sender"}
        recipient = {"harness": "codex", "session_id": None, "name": "fresh",
                     "pane_id": "w1:pX"}
        proof = {"schema": "peer-first-input-process-v1", "harness": "codex",
                 "pane_id": "w1:pX", "pid": 4242, "start": "123",
                 "pid_namespace": "pid:[77]"}
        _text, ref = peer_steward.peer_message.prepare_peer_message(
            "[steer] bounded first input", sender, recipient, defer=True,
            **({"first_input_process": proof} if process_proof else {}))
        if submitted:
            claim = peer_steward.peer_message.claim_pending_first_input(
                ref, {"harness": "codex", "pane_id": "w1:pX"}, proof)
            self.assertIsNotNone(claim)
            self.assertTrue(peer_steward.peer_message.mark_pending_prompt_queued(
                ref, claim["rpc_claim"], receipt="native-first-input-submitted"))
        store = peer_steward.peer_obligations.ObligationStore()
        duty = store.create("message-" + ref, "message",
            {"server": "default", "pane": "w1:pX", "harness": "codex", "session_id": ""},
            {"ref": ref, "target": "fresh", "to": recipient, "from": sender})
        return proof, ref, store, duty

    def test_replaced_foreground_cannot_bind_or_queue_and_records_typed_reason(self):
        proof, ref, store, duty = self._row_and_duty()
        changed = {**proof, "pid": 4243, "start": "456"}
        ident = {"harness": "codex", "session_id": "sid-new", "pane": "w1:pX"}
        with mock.patch.object(peer_steward, "_retire_target",
                               return_value=("idle", ident, 0, None)), \
             mock.patch.object(peer_steward, "_retire_pane_info", return_value=self._pane_info()), \
             mock.patch.object(peer_steward, "_prompt_process_session",
                               return_value=("sid-new", None)), \
             mock.patch.object(peer_steward, "_foreground_process_proof", return_value=changed), \
             mock.patch.object(peer_steward.peer_message, "bind_pending_recipient") as bind, \
             mock.patch.object(peer_steward.peer_message, "deliver_pending_codex") as queue, \
             mock.patch.object(peer_steward, "_ensure_sender_delay_notice") as notice:
            peer_steward._resume_message_obligation(duty, store)
        result = store.get(duty["id"])
        self.assertEqual((result["state"], result["observation"]["reason"]),
                         ("unknown", "target-process-changed"))
        self.assertEqual(peer_steward.peer_message._read_pending(ref)["state"], "pending")
        bind.assert_not_called()
        queue.assert_not_called()
        notice.assert_not_called()

    def test_replaced_foreground_uses_only_the_existing_bounded_sender_notice(self):
        proof, _ref, store, duty = self._row_and_duty()
        pending_ref = duty["intent"]["ref"]
        row = peer_steward.peer_message._read_pending(pending_ref)
        peer_steward.peer_message._save_pending(
            dict(row, created=time.time() - peer_steward._FLUSH_STUCK_HOURS * 3600 - 1))
        changed = {**proof, "pid": 4243, "start": "456"}
        ident = {"harness": "codex", "session_id": "sid-new", "pane": "w1:pX"}
        with mock.patch.object(peer_steward, "_retire_target",
                               return_value=("idle", ident, 0, None)), \
             mock.patch.object(peer_steward, "_retire_pane_info", return_value=self._pane_info()), \
             mock.patch.object(peer_steward, "_prompt_process_session",
                               return_value=("sid-new", None)), \
             mock.patch.object(peer_steward, "_foreground_process_proof", return_value=changed), \
             mock.patch.object(peer_steward, "_ensure_sender_delay_notice") as notice, \
             mock.patch.object(peer_steward.peer_message, "bind_pending_recipient") as bind, \
             mock.patch.object(peer_steward.peer_message, "deliver_pending_codex") as queue:
            peer_steward._resume_message_obligation(duty, store)
        notice.assert_called_once()
        bind.assert_not_called()
        queue.assert_not_called()
        self.assertEqual(store.get(duty["id"])["observation"]["reason"],
                         "target-process-changed")

    def test_old_sidless_row_without_process_proof_stays_unknown(self):
        _proof, ref, store, duty = self._row_and_duty(process_proof=False)
        ident = {"harness": "codex", "session_id": "sid-new", "pane": "w1:pX"}
        with mock.patch.object(peer_steward, "_retire_target",
                               return_value=("idle", ident, 0, None)), \
             mock.patch.object(peer_steward, "_retire_pane_info", return_value=self._pane_info()), \
             mock.patch.object(peer_steward, "_prompt_process_session",
                               return_value=("sid-new", None)), \
             mock.patch.object(peer_steward, "_foreground_process_proof") as current:
            peer_steward._resume_message_obligation(duty, store)
        self.assertEqual(store.get(duty["id"])["observation"]["reason"],
                         "target-process-proof-unknown")
        current.assert_not_called()
        self.assertIsNone(peer_steward.peer_message._read_pending(ref).get("bound_to"))

    def test_submitted_first_input_is_never_queued_again_after_sid_appears(self):
        _proof, ref, store, duty = self._row_and_duty(submitted=True)
        sidless = {"harness": "codex", "session_id": "-", "pane": "w1:pX"}
        with mock.patch.object(peer_steward, "_retire_target",
                               return_value=("idle", sidless, 0, None)) as target, \
             mock.patch.object(peer_steward.peer_message, "bind_pending_recipient") as bind, \
             mock.patch.object(peer_steward.peer_message, "deliver_pending_codex") as queue:
            peer_steward._resume_message_obligation(duty, store)
        result = store.get(duty["id"])
        self.assertEqual(result["state"], "unknown")
        self.assertEqual(result["observation"]["reason"], "native-first-input-submitted")
        target.assert_called_once_with("w1:pX")
        bind.assert_not_called()
        queue.assert_not_called()
        self.assertEqual(peer_steward.peer_message._read_pending(ref)["state"], "queued")

    def test_sidless_codex_obligation_binds_the_thread_its_pane_tui_proves(self):
        proof, ref, store, duty = self._row_and_duty()
        sidless = {"harness": "codex", "session_id": "-", "pane": "w1:pX"}
        delivered = []

        def deliver(ref_, *, timeout=1.0, start_interrupted=False):
            row = peer_steward.peer_message._read_pending(ref_)
            delivered.append(peer_steward.peer_message.pending_delivery_recipient(row)["session_id"])
            return {"status": "queued"}

        for thread, reason in ((None, "target-session-unresolved"), ("proven-thread", None)):
            info = self._pane_info()
            with self.subTest(thread=thread), \
                 mock.patch.object(peer_steward, "_retire_target",
                                   return_value=("idle", sidless, 0, None)), \
                 mock.patch.object(peer_steward, "_retire_pane_info", return_value=info), \
                 mock.patch.object(peer_steward, "_prompt_process_session",
                                   return_value=(thread, None)) as resolver, \
                 mock.patch.object(peer_steward, "_foreground_process_proof", return_value=proof), \
                 mock.patch.object(peer_steward, "_pane_readiness",
                                   return_value=SimpleNamespace(state="ready", reason="")), \
                 mock.patch.object(peer_steward.peer_message, "deliver_pending_codex", side_effect=deliver):
                peer_steward._resume_message_obligation(store.get(duty["id"]), store)
                resolver.assert_called_once_with("w1:pX", "codex", info)
                if reason:
                    self.assertEqual(store.get(duty["id"])["observation"]["reason"], reason)
        self.assertEqual(delivered, ["proven-thread"])
        self.assertEqual(peer_steward.peer_message._read_pending(ref)["bound_to"],
                         {"harness": "codex", "session_id": "proven-thread", "pane_id": "w1:pX"})

    def test_proven_thread_still_needs_the_original_foreground_process(self):
        proof, ref, store, duty = self._row_and_duty()
        changed = {**proof, "pid": 4243, "start": "456"}
        sidless = {"harness": "codex", "session_id": "-", "pane": "w1:pX"}
        with mock.patch.object(peer_steward, "_retire_target",
                               return_value=("idle", sidless, 0, None)), \
             mock.patch.object(peer_steward, "_retire_pane_info", return_value=self._pane_info()), \
             mock.patch.object(peer_steward, "_prompt_process_session",
                               return_value=("proven-thread", None)), \
             mock.patch.object(peer_steward, "_foreground_process_proof", return_value=changed), \
             mock.patch.object(peer_steward.peer_message, "deliver_pending_codex") as queue, \
             mock.patch.object(peer_steward, "_ensure_sender_delay_notice"):
            peer_steward._resume_message_obligation(duty, store)
        self.assertEqual(store.get(duty["id"])["observation"]["reason"], "target-process-changed")
        queue.assert_not_called()
        self.assertIsNone(peer_steward.peer_message._read_pending(ref).get("bound_to"))


class MessageTerminalObligationTest(_TmpRootMixin, unittest.TestCase):
    def _normal_delay_notice(self):
        pm = peer_steward.peer_message
        sender = {"harness": "claude", "session_id": "delay-sender", "name": None}
        recipient = {"harness": "codex", "session_id": "delay-recipient", "name": None}
        _text, original_ref = pm.prepare_peer_message(
            "original pending message", sender, recipient, defer=True)
        peer_steward._ensure_sender_delay_notice(pm._read_pending(original_ref))
        store = peer_steward.peer_obligations.ObligationStore()
        delay = store.get("delay-" + original_ref)
        notice_ref = delay["observation"]["transfer_ref"]
        return store, delay, pm._read_pending(notice_ref), sender, original_ref

    def test_received_delay_notice_closes_its_logical_duty_and_courier(self):
        store, delay, notice, recipient, original_ref = self._normal_delay_notice()
        pm = peer_steward.peer_message
        self.assertNotEqual(delay["intent"]["body_digest"], notice["body_sha256"])
        self.assertEqual(pm.receive_peer_message(notice["text"], recipient), 0)
        settled = store.get(delay["id"])
        self.assertEqual((settled["state"], settled["result"], settled["delivery"]),
                         ("complete", "received", "acknowledged"))
        self.assertEqual(store.get("message-" + notice["ref"])["state"], "complete")
        self.assertEqual(pm._read_pending(original_ref)["state"], "pending")
        revision = settled["revision"]
        received_text = pm._read_transfer_record(notice["ref"])
        self.assertIsNotNone(received_text)
        self.assertEqual(pm.receive_peer_message(notice["text"], recipient), 0)
        self.assertEqual(store.get(delay["id"])["revision"], revision)
        with mock.patch.object(peer_steward, "_resume_message_obligation") as resume:
            peer_steward._reconcile_obligations(store)
        resume.assert_not_called()

    def test_another_courier_ack_does_not_settle_mismatched_delay_duty(self):
        pm = peer_steward.peer_message
        store = peer_steward.peer_obligations.ObligationStore()
        original_ref = "e" * 32
        sender = {"harness": "codex", "session_id": "notice-sender"}
        recipient = {"harness": "claude", "session_id": "notice-recipient"}
        body = "notice with incomplete old intent"
        duty = store.create("delay-" + original_ref, "message", {"server": "default"},
            {"delay_notice_for": original_ref, "body": body, "body_digest": "0" * 64,
             "from": sender, "to": recipient, "target": recipient["session_id"]})
        text, ref = pm.prepare_peer_message(body, sender, recipient, defer=True,
            refs=["delay-notice:" + original_ref, original_ref])
        store.update(duty["id"], observation={"transfer_ref": ref})
        peer_steward._schedule_message_obligation(pm._read_pending(ref), recipient["session_id"],
                                                 delay_notice_for=original_ref)
        self.assertEqual(pm.receive_peer_message(text, recipient), 0)
        self.assertEqual(store.get("message-" + ref)["state"], "complete")
        peer_steward._resume_message_obligation(store.get(duty["id"]), store)
        current = store.get(duty["id"])
        self.assertEqual((current["state"], current["observation"]["reason"]),
                         ("unknown", "received-binding-unverified"))

    def test_delay_cancellation_validates_source_body_and_keeps_late_ack(self):
        store, delay, notice, recipient, _original_ref = self._normal_delay_notice()
        pm = peer_steward.peer_message
        identity = delay["intent"]["identity"]
        evidence = {"kind": "recipient-session-absent", "server": "default",
                    "harness": recipient["harness"], "session_id": recipient["session_id"],
                    "census_complete": True, "session_absent": True,
                    "census_endpoint": "default",
                    "recipient_binding": pm.pending_delivery_binding(notice, include_acked=True)}
        rejected = store.cancel_message_delivery(delay["id"],
            expected_revision=delay["revision"], ref=notice["ref"],
            body_digest=notice["body_sha256"], identity=identity,
            message=dict(notice, source_sha256="0" * 64), evidence=evidence)
        self.assertEqual(rejected["state"], "pending")
        self.assertEqual(pm.cancel_message_obligation(notice["ref"], duty_id=delay["id"],
            expected_revision=delay["revision"], identity=identity,
            message=notice, evidence=evidence), "cancelled")
        self.assertIsNone(store.get(delay["id"])["result"])
        self.assertEqual(pm.receive_peer_message(notice["text"], recipient), 0)
        current = store.get(delay["id"])
        self.assertEqual((current["state"], current["result"], current["delivery"]),
                         ("cancelled", None, "acknowledged"))

    def _queued_duty(self, *, sid, target_name, ref_seed):
        sender = {"harness": "claude", "session_id": "sender-session", "name": "sender"}
        recipient = {"harness": "claude", "session_id": sid, "name": target_name,
                     "pane_id": "w1:p" + ref_seed}
        _text, ref = peer_steward.peer_message.prepare_peer_message(
            "[steer] terminal check " + ref_seed, sender, recipient, defer=True)
        row = peer_steward.peer_message._read_pending(ref)
        row = dict(row, state="queued", receipt="hook-context-emitted")
        peer_steward.peer_message._save_pending(row)
        store = peer_steward.peer_obligations.ObligationStore()
        identity = {"server": "default", "pane": recipient["pane_id"],
                    "harness": recipient["harness"], "session_id": sid}
        duty = store.create("message-" + ref, "message", identity,
            {"ref": ref, "body_digest": row["body_sha256"], "target": target_name,
             "from": sender, "to": recipient})
        return store, duty, row, recipient

    def test_confirmed_missing_sid_closes_once_while_live_queued_sid_stays_open(self):
        old_store, old_duty, _old_row, old_recipient = self._queued_duty(
            sid="sid-old", target_name="old-peer", ref_seed="old")
        live_store, live_duty, _live_row, live_recipient = self._queued_duty(
            sid="sid-live", target_name="live-peer", ref_seed="live")
        self.assertEqual(old_store.root, live_store.root)
        absent = peer_steward.herdr_observation.PaneCensus("default", (), time.monotonic())
        def target(name):
            if name == "old-peer":
                return "agent-not-found", {"harness": "-", "session_id": "-",
                    "name": name, "pane": "-"}, 4, "agent-not-found"
            return "idle", {"harness": "claude", "session_id": "sid-live",
                    "name": name, "pane": live_recipient["pane_id"]}, 0, None

        def census(*, session=None):
            return absent if session == "default" else None

        # The live target resolves exactly, so its census is not needed. Only
        # the old target reaches the complete endpoint census.
        with mock.patch.object(peer_steward, "_retire_target", side_effect=target), \
             mock.patch.object(peer_steward.herdr_observation, "pane_census", side_effect=census), \
             mock.patch.object(peer_steward.peer_message, "deliver_pending_codex") as deliver:
            peer_steward._reconcile_obligations(old_store,
                [old_duty, live_duty])
        old = old_store.get(old_duty["id"])
        live = live_store.get(live_duty["id"])
        self.assertEqual(old["state"], "cancelled")
        self.assertEqual(old["result"], None)
        self.assertEqual(old["cleanup"], "complete")
        self.assertEqual(old["delivery"], "failed")
        self.assertEqual(live["state"], "unknown")
        self.assertEqual(live["delivery"], "pending")
        self.assertNotIn(old_duty["id"], {row["id"] for row in old_store.list()})
        deliver.assert_not_called()

        with mock.patch.object(peer_steward, "_resume_message_obligation") as resume:
            peer_steward._reconcile_obligations(old_store)
        self.assertEqual([call.args[0]["id"] for call in resume.call_args_list],
                         [live_duty["id"]])

    def test_queued_message_to_a_vanished_tmux_server_is_folded_like_a_vanished_herdr_pane(self):
        store, duty, row, recipient = self._queued_duty(
            sid="sid-gone", target_name="gone-peer", ref_seed="tmuxgone")
        gone = "%0@" + os.path.join(tempfile.mkdtemp(), "no-such.sock")
        recipient["pane_id"] = gone
        identity = dict(duty["intent"]["identity"], pane=gone)
        evidence = None
        with mock.patch.object(peer_steward.herdr_observation, "pane_census", return_value=None):
            evidence = peer_steward._message_terminal_evidence(
                {"intent": {"identity": identity}}, dict(row, to=recipient),
                {"harness": "-", "session_id": "-", "pane": "-"}, "agent-not-found")
        self.assertEqual(evidence["kind"], "recipient-session-absent")
        self.assertTrue(evidence["session_absent"])
        # A live tmux server is not decided here: the unavailable census stays unknown.
        listener = socket.socket(socket.AF_UNIX)
        path = os.path.join(tempfile.mkdtemp(), "live.sock")
        listener.bind(path)
        listener.listen(1)
        self.addCleanup(listener.close)
        with mock.patch.object(peer_steward.herdr_observation, "pane_census", return_value=None):
            self.assertIsNone(peer_steward._message_census("default", "%0@" + path))

    def test_incomplete_or_foreign_session_census_preserves_unknown(self):
        cases = (
            (None, "census-unavailable"),
            (peer_steward.herdr_observation.PaneCensus("other", (), time.monotonic()), "wrong-endpoint"),
            (peer_steward.herdr_observation.PaneCensus("default", (
                {"pane_id": "w1:pOther", "agent_session": {"value": "sid-old"}},),
                time.monotonic()), "sid-still-present"),
        )
        for census, case in cases:
            with self.subTest(case=case):
                store, duty, _row, _recipient = self._queued_duty(
                    sid="sid-old", target_name="old-peer", ref_seed=case.replace("-", "")[:8])
                with mock.patch.object(peer_steward, "_retire_target",
                        return_value=("agent-not-found", {"harness": "-", "session_id": "-",
                            "name": "old-peer", "pane": "-"}, 4, "agent-not-found")), \
                     mock.patch.object(peer_steward.herdr_observation, "pane_census", return_value=census):
                    peer_steward._resume_message_obligation(duty, store)
                latest = store.get(duty["id"])
                self.assertEqual(latest["state"], "unknown")
                self.assertNotIn("terminal_evidence", latest["observation"])

    def test_historical_exact_pending_receipt_closes_and_is_excluded_next_pass(self):
        sender = {"harness": "claude", "session_id": "legacy-sender", "name": "sender"}
        recipient = {"harness": "codex", "session_id": "legacy-receiver", "name": "worker"}
        text, ref = peer_steward.peer_message.prepare_peer_message(
            "[steer] historical exact receipt", sender, recipient, defer=True)
        self.assertEqual(peer_steward.peer_message.receive_peer_message(text, recipient), 0)

        # Match the real version-1 layout written before transfer refs and the
        # derived exact_receiver_receipt field existed.
        transfer_path = peer_steward.peer_message._transfer_path(ref)
        transfer = json.loads(transfer_path.read_text(encoding="utf-8"))
        transfer.pop("refs", None)
        transfer_path.write_text(json.dumps(transfer), encoding="utf-8")
        row = peer_steward.peer_message._read_pending(ref)
        row.pop("exact_receiver_receipt", None)
        peer_steward.peer_message._save_pending(row)

        store = peer_steward.peer_obligations.ObligationStore()
        duty = store.create("message-" + ref, "message",
            {"server": "default", "pane": "", **recipient},
            {"ref": ref, "body_digest": row["body_sha256"], "from": sender,
             "to": row["to"], "refs": row["refs"]})
        peer_steward._resume_message_obligation(duty, store)
        settled = store.get(duty["id"])
        self.assertEqual((settled["state"], settled["result"], settled["delivery"]),
                         ("complete", "received", "acknowledged"))
        self.assertEqual(settled["observation"]["receiver_receipt"]["ref"], ref)

        with mock.patch.object(peer_steward, "_resume_message_obligation",
                               wraps=peer_steward._resume_message_obligation) as resume:
            peer_steward._reconcile_obligations(store)
        resume.assert_not_called()

    def test_codex_and_opencode_native_callbacks_ack_old_transfer_refs(self):
        import codex_queue_delivery as queue
        import codex_queue_dispatch as queue_dispatch

        for harness in ("codex", "opencode"):
            with self.subTest(harness=harness):
                sender = {"harness": "claude", "session_id": "native-old-sender",
                          "name": "sender"}
                recipient = {"harness": harness, "session_id": "native-old-" + harness,
                             "name": "worker"}
                _text, ref = peer_steward.peer_message.prepare_peer_message(
                    "[steer] late native ACK on old transfer", sender, recipient, defer=True)
                transfer_path = peer_steward.peer_message._transfer_path(ref)
                transfer = json.loads(transfer_path.read_text(encoding="utf-8"))
                transfer.pop("refs", None)
                transfer_path.write_text(json.dumps(transfer), encoding="utf-8")
                row = peer_steward.peer_message._read_pending(ref)
                store = peer_steward.peer_obligations.ObligationStore()
                duty = store.create("message-" + ref, "message",
                    {"server": "default", "pane": "", **recipient},
                    {"ref": ref, "body_digest": row["body_sha256"], "from": sender,
                     "to": row["to"], "refs": row["refs"]})

                if harness == "codex":
                    with mock.patch.object(queue_dispatch, "resolve_queue_socket",
                                           return_value="fixture-socket"), \
                         mock.patch.object(queue, "send_at_least_once",
                                           return_value={"status": "consumed"}):
                        result = peer_steward.peer_message.deliver_pending_codex(ref)
                    self.assertEqual(result["status"], "received")
                    expected_id = "delivery-peer-" + ref
                else:
                    args = peer_steward.peer_message.argparse.Namespace(
                        to_harness=harness, to_session_id=recipient["session_id"],
                        claim=None, queued=ref, resuming=None, received=None,
                        message_id="native-message-" + harness)
                    self.assertEqual(peer_steward.peer_message.cmd_pending(args), 0)
                    wrong_id = peer_steward.peer_message.argparse.Namespace(
                        to_harness=harness, to_session_id=recipient["session_id"],
                        claim=None, queued=None, resuming=None, received=ref,
                        message_id="foreign-native-message")
                    self.assertEqual(peer_steward.peer_message.cmd_pending(wrong_id), 1)
                    wrong_recipient = peer_steward.peer_message.argparse.Namespace(
                        to_harness=harness, to_session_id="foreign-native-recipient",
                        claim=None, queued=None, resuming=None, received=ref,
                        message_id=args.message_id)
                    self.assertEqual(peer_steward.peer_message.cmd_pending(wrong_recipient), 1)
                    received = peer_steward.peer_message.argparse.Namespace(
                        to_harness=harness, to_session_id=recipient["session_id"],
                        claim=None, queued=None, resuming=None, received=ref,
                        message_id=args.message_id)
                    self.assertEqual(peer_steward.peer_message.cmd_pending(received), 0)
                    expected_id = args.message_id

                pending = peer_steward.peer_message._read_pending(ref)
                self.assertEqual((pending["state"], pending["receipt"],
                                  pending["actual_message_id"]),
                                 ("received", "exact-native-history", expected_id))
                self.assertEqual(pending["exact_receiver_receipt"]["actual_message_id"],
                                 expected_id)
                settled = store.get(duty["id"])
                self.assertEqual((settled["state"], settled["result"], settled["delivery"]),
                                 ("complete", "received", "acknowledged"))

    def test_exact_received_ledger_recovers_missing_pending_row(self):
        sender = {"harness": "claude", "session_id": "sender-session", "name": "sender"}
        recipient = {"harness": "codex", "session_id": "recipient-session", "name": "worker"}
        text, ref = peer_steward.peer_message.prepare_peer_message(
            "[steer] recovered exact receipt", sender, recipient, defer=True)
        pending = peer_steward.peer_message._read_pending(ref)
        self.assertEqual(peer_steward.peer_message.receive_peer_message(text, recipient), 0)
        peer_steward.peer_message._pending_path(ref).unlink()

        store = peer_steward.peer_obligations.ObligationStore()
        duty = store.create("message-" + ref, "message",
            {"server": "default", "pane": "", "harness": "codex",
             "session_id": "recipient-session"},
            {"ref": ref, "body_digest": pending["body_sha256"],
             "from": sender, "to": pending["to"], "refs": pending["refs"]})
        store.update(duty["id"], state="unknown",
                     observation={"ref": ref, "reason": "pending-row-missing"})

        peer_steward._resume_message_obligation(store.get(duty["id"]), store)
        recovered = store.get(duty["id"])
        self.assertEqual((recovered["state"], recovered["result"], recovered["delivery"]),
                         ("complete", "received", "acknowledged"))
        self.assertEqual(recovered["observation"]["receiver_receipt"]["ref"], ref)
        notice = peer_steward.peer_message._peer_notice_record(ref, recipient)
        receipt = recovered["observation"]["receiver_receipt"]
        self.assertEqual(receipt["notice_body_sha256"], notice["body_sha256"])
        self.assertEqual(receipt["recipient_binding"], notice["receiver_binding"])

    def test_missing_pending_recovery_rejects_generic_and_incomplete_or_foreign_evidence(self):
        sender = {"harness": "claude", "session_id": "sender-session", "name": "sender"}
        recipient = {"harness": "codex", "session_id": "recipient-session", "name": "worker"}
        text, ref = peer_steward.peer_message.prepare_peer_message(
            "[steer] receipt evidence negatives", sender, recipient, defer=True)
        pending = peer_steward.peer_message._read_pending(ref)
        store = peer_steward.peer_obligations.ObligationStore()
        identity = {"server": "default", "pane": "", "harness": "codex",
                    "session_id": "recipient-session"}
        self.assertEqual(peer_steward.peer_message.receive_peer_message(text, recipient), 0)
        peer_steward.peer_message._pending_path(ref).unlink()
        valid = peer_steward.peer_message._peer_notice_record(ref, recipient)
        self.assertIsNotNone(valid)
        duty = store.create("message-" + ref, "message", identity,
            {"ref": ref, "body_digest": pending["body_sha256"], "from": sender,
             "to": pending["to"], "refs": pending["refs"]})
        variants = {
            "wrong-receipt-type": {**valid, "delivery": {**valid["delivery"],
                                                               "receipt": "exact-native-history"}},
            "wrong-provenance-type": {**valid, "receipt_provenance": "caller-supplied"},
            "wrong-transfer-body-digest": {**valid,
                "received_transfer_body_sha256": "0" * 64},
            "foreign-sender": {**valid, "from": {**valid["from"],
                                                     "session_id": "foreign-sender"}},
            "foreign-recipient": {**valid, "to": {**valid["to"],
                                                       "session_id": "foreign-recipient"}},
            "foreign-ref": {**valid, "transfer_ref": "f" * 32},
            "foreign-generation": {**valid, "receiver_binding": {
                **valid["receiver_binding"], "recipient": {
                    "harness": "codex", "session_id": "foreign-recipient"}}},
            "legacy-incomplete": {key: value for key, value in valid.items()
                                   if key not in {"receipt_provenance",
                                                  "received_transfer_body_sha256",
                                                  "receiver_binding"}},
        }
        for name, malformed in variants.items():
            with self.subTest(evidence=name), mock.patch.object(
                    peer_steward.peer_message, "_peer_notice_record", return_value=malformed):
                peer_steward._resume_message_obligation(store.get(duty["id"]), store)
                latest = store.get(duty["id"])
                self.assertEqual(latest["state"], "unknown")
                self.assertEqual(latest["observation"]["reason"], "pending-row-missing")

        other_text, other_ref = peer_steward.peer_message.prepare_peer_message(
            "[steer] generic notice is not receipt", sender, recipient, defer=True)
        other_pending = peer_steward.peer_message._read_pending(other_ref)
        other = store.create("message-" + other_ref, "message", identity,
            {"ref": other_ref, "body_digest": other_pending["body_sha256"],
             "from": sender, "to": other_pending["to"], "refs": other_pending["refs"]})
        peer_steward.peer_message._pending_path(other_ref).unlink()
        self.assertEqual(peer_steward.peer_message.cmd_record(
            peer_steward.peer_message.argparse.Namespace(
                from_harness=sender["harness"], from_session_id=sender["session_id"],
                from_name=sender["name"], from_project="", to_harness=recipient["harness"],
                to_session_id=recipient["session_id"], to_name=None, kind="notice",
                surface="herdr", status="received", receipt="exact-peer-ref",
                ref=[other_ref], transfer_ref=other_ref, body_file=None,
                body_stdin=False, body_text="caller says received")), 0)
        peer_steward._resume_message_obligation(other, store)
        latest = store.get(other["id"])
        self.assertEqual(latest["state"], "unknown")
        self.assertEqual(latest["observation"]["reason"], "pending-row-missing")

    def test_missing_pending_row_without_exact_receipt_remains_unknown(self):
        sender = {"harness": "claude", "session_id": "sender-session", "name": "sender"}
        recipient = {"harness": "codex", "session_id": "recipient-session", "name": "worker"}
        _text, ref = peer_steward.peer_message.prepare_peer_message(
            "[steer] missing row without receipt", sender, recipient, defer=True)
        pending = peer_steward.peer_message._read_pending(ref)
        peer_steward.peer_message._pending_path(ref).unlink()
        store = peer_steward.peer_obligations.ObligationStore()
        duty = store.create("message-" + ref, "message",
            {"server": "default", "pane": "", "harness": "codex",
             "session_id": "recipient-session"},
            {"ref": ref, "body_digest": pending["body_sha256"],
             "from": sender, "to": pending["to"]})

        peer_steward._resume_message_obligation(duty, store)
        latest = store.get(duty["id"])
        self.assertEqual(latest["state"], "unknown")
        self.assertEqual(latest["observation"]["reason"], "pending-row-missing")

    def test_live_rpc_claim_blocks_terminal_cancellation(self):
        store, duty, row, recipient = self._queued_duty(
            sid="sid-claimed", target_name="claimed-peer", ref_seed="claim")
        claim = {"token": "live-claim", "pid": os.getpid(), "start": "123"}
        row = dict(row, state="unverified", rpc_claim=claim)
        peer_steward.peer_message._save_pending(row)
        missing = {"harness": "-", "session_id": "-", "name": "claimed-peer", "pane": "-"}
        census = peer_steward.herdr_observation.PaneCensus("default", (), time.monotonic())
        with mock.patch.object(peer_steward, "_retire_target",
                return_value=("agent-not-found", missing, 4, "agent-not-found")), \
             mock.patch.object(peer_steward.herdr_observation, "pane_census", return_value=census), \
             mock.patch("dispatch_contract.process_identity_disposition", return_value="live"):
            peer_steward._resume_message_obligation(duty, store)

        latest = store.get(duty["id"])
        current = peer_steward.peer_message._read_pending(duty["intent"]["ref"])
        self.assertEqual(latest["state"], "unknown")
        self.assertEqual(latest["observation"]["reason"], "delivery-inflight")
        self.assertEqual(current["rpc_claim"], claim)
        self.assertEqual(current["state"], "unverified")

    def test_dead_pane_bound_process_closes_but_unresolved_process_does_not(self):
        for disposition, expected in (("dead", "cancelled"), ("unresolved", "unknown")):
            with self.subTest(disposition=disposition):
                sender = {"harness": "claude", "session_id": "sender-session", "name": "sender"}
                proof = {"schema": "peer-first-input-process-v1", "harness": "codex",
                         "pane_id": "w1:pBound", "pid": 99123, "start": "77",
                         "pid_namespace": os.readlink("/proc/self/ns/pid")}
                recipient = {"harness": "codex", "session_id": None,
                             "name": "bound-peer", "pane_id": "w1:pBound"}
                _text, ref = peer_steward.peer_message.prepare_peer_message(
                    "[steer] pane-bound " + disposition, sender, recipient,
                    defer=True, first_input_process=proof)
                row = peer_steward.peer_message._read_pending(ref)
                row = dict(row, state="queued", receipt="native-first-input-submitted")
                peer_steward.peer_message._save_pending(row)
                store = peer_steward.peer_obligations.ObligationStore()
                identity = {"server": "default", "pane": "w1:pBound",
                            "harness": "codex", "session_id": ""}
                duty = store.create("message-" + ref, "message", identity,
                    {"ref": ref, "body_digest": row["body_sha256"], "target": "bound-peer",
                     "from": sender, "to": recipient})
                with mock.patch.object(peer_steward, "_retire_target",
                        return_value=("idle", {"harness": "codex", "session_id": "new-sid",
                            "name": "bound-peer", "pane": "w1:pBound"}, 0, None)), \
                     mock.patch("dispatch_contract.process_identity_disposition",
                                return_value=disposition):
                    peer_steward._resume_message_obligation(duty, store)
                self.assertEqual(store.get(duty["id"])["state"], expected)


class NormalProducerRunnerBindingTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.state = self.base / "state"
        self.jobs = self.base / "dispatch" / "jobs.log"
        self.env = {"AGENT_PEER_LEDGER_ROOT": str(self.state),
                    "AGENT_DISPATCH_JOBS": str(self.jobs),
                    "HOME": str(self.base / "home")}
        Path(self.env["HOME"]).mkdir()
        patch = mock.patch.dict(os.environ, self.env, clear=True)
        patch.start()
        self.addCleanup(patch.stop)

    def _exact_retire(self):
        return {"server": "default", "pane": "fixture-pane", "harness": "codex",
                "session_id": "sid-retire", "name": "retire-target",
                "foreground": {"pid": 123, "start": "456"}}

    def test_message_and_delay_producers_use_the_shared_binding_helper(self):
        sender = {"harness": "claude", "session_id": "sid-sender", "name": "sender"}
        recipient = {"harness": "codex", "session_id": "sid-recipient", "name": "recipient"}
        message = {"ref": "a" * 32, "body_sha256": "b" * 64,
                   "from": sender, "to": recipient, "refs": []}
        with mock.patch.object(peer_steward.peer_obligations,
                               "ensure_normal_runner") as ensure:
            duty = peer_steward._schedule_message_obligation(message, "recipient", "fixture-pane")
            self.assertIsNotNone(duty)
            ensure.assert_called_once_with()

        delay_row = {"ref": "c" * 32, "created": time.time(), "refs": [],
                     "from": sender, "to": recipient}
        prepared = {"ref": "d" * 32, "body_sha256": "e" * 64,
                    "from": recipient, "to": sender, "refs": ["delay-notice:" + delay_row["ref"]]}
        with mock.patch.object(peer_steward.peer_obligations,
                               "ensure_normal_runner") as ensure, \
             mock.patch.object(peer_steward.peer_message, "prepare_peer_message",
                               return_value=("fixture text", prepared["ref"])), \
             mock.patch.object(peer_steward.peer_message, "_read_pending",
                               return_value=prepared):
            duty = peer_steward._ensure_sender_delay_notice(delay_row)
            self.assertIsNotNone(duty)
            self.assertEqual(ensure.call_count, 2)

    def test_retire_and_handoff_retire_producers_use_the_shared_binding_helper(self):
        exact = self._exact_retire()
        requester = {"session_id": "sid-requester", "harness": "codex",
                     "pane": "requester-pane", "server": "default"}
        with mock.patch.object(peer_steward.peer_obligations,
                               "ensure_normal_runner") as ensure:
            _store, duty = peer_steward._retire_request(
                "retire-target", exact, requester=requester)
            self.assertEqual(duty["intent"]["identity"], exact)
            ensure.assert_called_once_with()

        with mock.patch.object(peer_steward.peer_obligations,
                               "ensure_normal_runner") as ensure:
            handoff_exact = {**exact, "pane": "handoff-pane"}
            duty_id = peer_steward.reserve_handoff_retire(
                "handoff-target", handoff_exact, requester)
            self.assertTrue(duty_id.startswith("retire-"))
            self.assertEqual(ensure.call_count, 2)


class PromptDraftGuardTest(_TmpRootMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        for key in ("AGENT_DISPATCH_CALLER_HARNESS", "AGENT_DISPATCH_CURRENT_HARNESS",
                    "CLAUDE_SESSION_ID", "CODEX_SESSION_ID", "OPENCODE_SESSION_ID"):
            os.environ.pop(key, None)
        os.environ["CLAUDE_CODE_SESSION_ID"] = "sid-steward"
        self.harness, self.sid = "claude", "sid-child"
        self.readiness = type("Ready", (), {"state": "ready", "reason": ""})()
        self.enterContext(mock.patch.object(peer_steward.shutil, "which", return_value="/fixture/herdr"))
        self.enterContext(mock.patch.object(peer_steward.subprocess, "run", side_effect=self.run_herdr))
        self.enterContext(mock.patch.object(peer_steward, "_read_screen",
                                            side_effect=AssertionError("peer delivery read composer")))
        self.enterContext(mock.patch.object(peer_steward, "_pane_readiness",
                                            side_effect=lambda *a, **k: self.readiness))
        info = {"pane_id": "w1:pX", "shell_pid": os.getpid(),
                "foreground_process_group_id": os.getpid(),
                "foreground_processes": [{"pid": 456, "argv": ["claude"]}]}
        self.enterContext(mock.patch.object(peer_steward, "_retire_pane_info",
                                            return_value=info))
        self.enterContext(mock.patch.object(peer_steward, "_prompt_process_session",
                                            side_effect=lambda pane, harness, _info:
                                            (self.sid, None)))
        self.enterContext(mock.patch.object(
            peer_steward.herdr_observation, "pane_census",
            side_effect=lambda session=None: SimpleNamespace(
                endpoint=peer_steward.herdr_observation.endpoint_name(session), complete=True,
                panes=({"pane_id": "w1:pX", "agent": self.harness,
                        "agent_session": {"kind": "id", "value": self.sid},
                        "agent_status": "working", "cwd": "/fixture"},))))
        self.enterContext(mock.patch.object(peer_steward.peer_obligations, "ensure_normal_runner",
                                            return_value=False))
        self.enterContext(mock.patch("builtins.print"))

    def run_herdr(self, argv, **kwargs):
        if argv[:3] == ["herdr", "pane", "list"]:
            return _herdr_json({"result": {"panes": [{
                "pane_id": "w1:pX", "agent": self.harness,
                "agent_session": {"kind": "id", "value": self.sid},
                "agent_status": "working", "cwd": "/fixture"}]}})
        if argv[:3] == ["herdr", "agent", "list"]:
            return _herdr_json({"result": {"agents": [
                {"agent": self.harness, "agent_status": "working", "name": "child",
                 "pane_id": "w1:pX", "agent_session": {
                     "agent": self.harness, "kind": "id", "value": self.sid}}
            ]}})
        if argv[:3] == ["herdr", "agent", "get"]:
            return _herdr_json(_agent_json(self.harness, self.sid, "child", status="working"))
        return subprocess.CompletedProcess(argv, 0, stdout="{}", stderr="")

    def prompt(self, body="peer body"):
        return peer_steward.main(["prompt", "child", body])

    def test_non_codex_peer_prompt_retains_exact_ref_for_native_callback(self):
        self.assertEqual(self.prompt("[steer] native callback body"), 3)
        ref = self._all_records()[-1]["transfer_ref"]
        row = peer_steward.peer_message._read_pending(ref)
        self.assertEqual(row["state"], "pending")
        self.assertIn("native callback body", row["text"])
        self.assertEqual(row["to"], {"harness": "claude", "session_id": self.sid,
                                     "name": "child", "pane_id": "w1:pX"})

    def test_registered_work_readiness_withholds_queue_or_native_callback_delivery(self):
        self.readiness = type("NotReady", (), {
            "state": "pending", "reason": "bound-registered-work-pending"})()
        queue = mock.Mock()
        with mock.patch.object(peer_steward.peer_message, "deliver_pending_codex", queue):
            self.assertEqual(self.prompt("keep execution ownership"), 3)
        queue.assert_not_called()
        row = peer_steward.peer_message._read_pending(self._all_records()[-1]["transfer_ref"])
        self.assertEqual((row["state"], row["receipt"]), ("pending", "bound-registered-work-pending"))

    def test_codex_queue_does_not_read_or_type_into_composer(self):
        self.harness, self.sid = "codex", "thread-exact"
        accepted = []
        gates = []
        def queue(ref, *, start_interrupted=False):
            accepted.append(peer_steward.peer_message._read_pending(ref))
            gates.append(start_interrupted)
            return {"status": "queued", "reason": "native-queue-accepted"}
        # The queue path never types into the composer. It reads the visible
        # screen once, read-only, as the sender-side gate for an interrupted thread.
        with mock.patch.object(peer_steward.peer_message, "deliver_pending_codex", side_effect=queue), \
             mock.patch.object(peer_steward, "_read_screen",
                               return_value=peer_steward._screen_lines(CODEX_EMPTY)):
            self.assertEqual(self.prompt("[steer] exact queue"), 3)
        self.assertEqual(gates, [True])
        self.assertEqual(len(accepted), 1)
        self.assertEqual(accepted[0]["to"]["session_id"], "thread-exact")
        self.assertEqual(accepted[0]["state"], "pending")
        self.assertEqual(accepted[0]["text"].splitlines()[0], "[steer] exact queue")

    def test_reconnected_obligation_keeps_claude_payload_pending_without_ptysend(self):
        self.assertEqual(self.prompt("parent duty stays sealed"), 3)
        ref = self._all_records()[-1]["transfer_ref"]
        store = peer_steward.peer_obligations.ObligationStore()
        duty = store.get("message-" + ref)
        self.assertIsNotNone(duty)
        with mock.patch.object(peer_steward, "_retire_target", return_value=("idle", {
                "harness": "claude", "session_id": self.sid, "pane": "w1:pX", "name": "child"}, 0, None)), \
             mock.patch.object(peer_steward, "_herdr_prompt",
                               side_effect=AssertionError("obligation runner typed into pane")):
            peer_steward._resume_message_obligation(duty, store)
        self.assertEqual(peer_steward.peer_message._read_pending(ref)["state"], "pending")
        self.assertEqual(store.get(duty["id"])["state"], "pending")

    def test_no_trailer_and_no_verify_arguments_cannot_reinstate_screen_transport(self):
        parser = peer_steward.build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(["prompt", "child", "body", "--no-verify"])
        self.assertEqual(self.prompt("ordinary native body"), 3)
        row = peer_steward.peer_message._read_pending(self._all_records()[-1]["transfer_ref"])
        self.assertEqual(row["state"], "pending")


class F100cStewardModeTest(_TmpRootMixin, unittest.TestCase):
    def test_steward_on_off_raises_and_clears_the_flag(self):
        os.environ["CLAUDE_CODE_SESSION_ID"] = "sid-steward"
        with mock.patch("builtins.print") as print_mock:
            self.assertEqual(peer_steward.main(["steward", "on"]), 0)
        self.assertIn("steward=on", print_mock.call_args[0][0])
        markers = peer_steward.peer_message.read_steward_markers()
        self.assertIn(("claude", "sid-steward"), markers)
        entry = markers[("claude", "sid-steward")]["targets"]["-"]
        self.assertEqual((entry["kind"], entry["source"], entry["session_id"]), ("explicit", "explicit", None))
        with mock.patch("builtins.print") as print_mock:
            self.assertEqual(peer_steward.main(["steward", "off"]), 0)
        self.assertIn("steward=off", print_mock.call_args[0][0])
        self.assertEqual(peer_steward.peer_message.read_steward_markers(), {})

    def test_steward_on_without_identity_is_a_typed_failure(self):
        with mock.patch("builtins.print") as print_mock:
            self.assertEqual(peer_steward.main(["steward", "on"]), 1)
        self.assertIn("reason=no-session-identity", print_mock.call_args[0][0])


class FromNameTest(_TmpRootMixin, unittest.TestCase):
    """C-3 — `_from_name` reads Claude via `claude_session_name` (unchanged) and
    Codex/OpenCode via `tools/fleet/session_registry.py` (B-1), the same lazy
    `tools/` sys.path trick `peer-message.py:412 steward_marker_roots` uses for
    non-Fleet consumers. Any other harness, or any failure along the way, is
    `None` — a name is never guessed."""

    def test_unknown_harness_and_missing_registry_record_are_none(self):
        self.assertIsNone(peer_steward._from_name("unknown", "sid-1"))
        with tempfile.TemporaryDirectory() as registry_dir:
            os.environ["FLEET_SESSION_REGISTRY_DIR"] = registry_dir
            self.assertIsNone(peer_steward._from_name("codex", "no-such-session"))
            self.assertIsNone(peer_steward._from_name("opencode", "no-such-session"))

    def test_codex_and_opencode_resolve_via_session_registry(self):
        with tempfile.TemporaryDirectory() as registry_dir:
            os.environ["FLEET_SESSION_REGISTRY_DIR"] = registry_dir
            for harness, pid, sid, name in (
                ("codex", 111, "codex-sess-1", "hearting-codex-1"),
                ("opencode", 222, "oc-sess-1", "hearting-oc-1"),
            ):
                d = Path(registry_dir) / harness
                d.mkdir(parents=True)
                (d / ("%d.json" % pid)).write_text(json.dumps(
                    {"pid": pid, "sessionId": sid, "name": name, "harness": harness}))
                self.assertEqual(peer_steward._from_name(harness, sid), name)


class FromNameBareSubprocessTest(unittest.TestCase):
    """C-3 — the lazy `tools/` sys.path insert must work in the actual condition
    a real herdr-launched `peer-steward.py` subprocess runs under: a cwd outside
    the repository and no inherited `PYTHONPATH`. `test_importable_with_only_
    tools_on_sys_path` (B-1) proves this for `session_registry` alone; this
    proves the full `peer-steward.py::_from_name` codex path reaches it too."""

    def test_from_name_resolves_a_codex_name_in_a_bare_subprocess(self):
        with tempfile.TemporaryDirectory() as registry_dir, \
             tempfile.TemporaryDirectory() as outside_cwd:
            pid = 424242
            record_dir = Path(registry_dir) / "codex"
            record_dir.mkdir(parents=True)
            (record_dir / ("%d.json" % pid)).write_text(json.dumps({
                "pid": pid, "sessionId": "codex-sess-c3", "name": "hearting-codex-c3",
                "harness": "codex",
            }))
            script = (
                "import importlib.util\n"
                "spec = importlib.util.spec_from_file_location('peer_steward', %r)\n"
                "mod = importlib.util.module_from_spec(spec)\n"
                "spec.loader.exec_module(mod)\n"
                "print(mod._from_name('codex', 'codex-sess-c3') or '')\n"
            ) % str(_HERE / "peer-steward.py")
            env = dict(os.environ)
            env.pop("PYTHONPATH", None)
            env["FLEET_SESSION_REGISTRY_DIR"] = registry_dir
            proc = subprocess.run([sys.executable, "-c", script], cwd=outside_cwd,
                                  capture_output=True, text=True, env=env, timeout=10)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "hearting-codex-c3")


class CurrentSessionIdentityDelegationTest(unittest.TestCase):
    """F-<next> fleet-route-chain-r2 plan §3 B-3: `_current_session_identity` delegates to
    `dispatch_parent_completion.interactive_parent_identity` first, keeping the prior
    AGENT_SESSION_ID fallback for the ambiguous/unset case; a native id is never guessed."""

    _ENV_KEYS = ("CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID", "CODEX_THREAD_ID",
                 "CODEX_SESSION_ID", "OPENCODE_SESSION_ID", "AGENT_SESSION_ID",
                 "AGENT_DISPATCH_CALLER_HARNESS", "AGENT_DISPATCH_CURRENT_HARNESS",
                 "HERDR_PANE_ID")

    def _clean_env(self, **overrides):
        env = {key: None for key in self._ENV_KEYS}
        env.update(overrides)
        return mock.patch.dict(os.environ, {k: v for k, v in env.items() if v is not None},
                               clear=False)

    def setUp(self):
        for key in self._ENV_KEYS:
            os.environ.pop(key, None)
        self.addCleanup(lambda: [os.environ.pop(k, None) for k in self._ENV_KEYS])

    def test_explicit_caller_harness_wins(self):
        with self._clean_env(CLAUDE_CODE_SESSION_ID="sid-c", CODEX_THREAD_ID="sid-x",
                             AGENT_DISPATCH_CALLER_HARNESS="codex"):
            self.assertEqual(peer_steward._current_session_identity(), ("sid-x", "codex"))

    def test_ambiguous_multiple_sessions_records_unknown_sender(self):
        # No explicit caller harness + two sessions set -> interactive_parent_identity()
        # raises caller-harness-ambiguous. The sender is unknown, never the first of the
        # claude > codex > opencode guesses (that named a Codex thread `claude [bc]`).
        with self._clean_env(CLAUDE_CODE_SESSION_ID="sid-c", CODEX_THREAD_ID="sid-x"):
            self.assertEqual(peer_steward._current_session_identity(), ("", "unknown"))

    def test_ambiguous_env_keeps_the_agent_session_id_fallback(self):
        with self._clean_env(CLAUDE_CODE_SESSION_ID="sid-c", CODEX_THREAD_ID="sid-x",
                             AGENT_SESSION_ID="sid-legacy"):
            self.assertEqual(peer_steward._current_session_identity(), ("sid-legacy", "unknown"))

    def test_explicit_name_without_its_sid_never_takes_foreign_sid(self):
        with self._clean_env(CLAUDE_CODE_SESSION_ID="sid-c", AGENT_DISPATCH_CALLER_HARNESS="codex"):
            self.assertEqual(peer_steward._current_session_identity(), ("", "unknown"))

    def test_invalid_explicit_name_never_takes_a_native_sid(self):
        with self._clean_env(CODEX_THREAD_ID="sid-x", AGENT_DISPATCH_CALLER_HARNESS="gemini"):
            self.assertEqual(peer_steward._current_session_identity(), ("", "unknown"))

    def test_single_session_delegates_cleanly(self):
        with self._clean_env(CODEX_THREAD_ID="sid-solo"):
            self.assertEqual(peer_steward._current_session_identity(), ("sid-solo", "codex"))

    def test_agent_session_id_still_falls_back_to_unknown(self):
        with self._clean_env(AGENT_SESSION_ID="sid-legacy"):
            self.assertEqual(peer_steward._current_session_identity(), ("sid-legacy", "unknown"))

    def test_nothing_set_returns_empty_unknown(self):
        with self._clean_env():
            self.assertEqual(peer_steward._current_session_identity(), ("", "unknown"))


class CodexPromptGateAndPeerStatusTest(_TmpRootMixin, unittest.TestCase):
    sender = {"harness": "claude", "session_id": "sender-session", "name": "sender"}

    # --- sender-side screen gate for an interrupted Codex thread ---
    def gate(self, screen):
        lines = None if screen is None else peer_steward._screen_lines(screen)
        with mock.patch.object(peer_steward, "_read_screen", return_value=lines):
            return peer_steward._codex_interrupt_start_gate("w1:pX")

    def test_gate_maps_the_visible_composer_to_one_of_four_values(self):
        popup = "banner: project · main\nPrior chat text\n" + _codex_snapshot("worktrees_new_choices.snap")
        trust = _codex_snapshot("startup_hooks_review_prompt.snap")
        self.assertIs(self.gate(CODEX_EMPTY), True)
        self.assertEqual(self.gate(CODEX_DRAFT), "draft")
        self.assertEqual(self.gate(popup), "form")
        self.assertEqual(self.gate(trust), "form")
        self.assertEqual(self.gate(None), "screen-unknown")
        self.assertEqual(self.gate(NO_BOX), "screen-unknown")
        with mock.patch.object(peer_steward, "_read_screen") as read:
            self.assertEqual(peer_steward._codex_interrupt_start_gate(None), "screen-unknown")
        read.assert_not_called()

    # --- status: the sender sees its own undelivered peer rows ---
    def setUp(self):
        super().setUp()
        self.pm = peer_steward.peer_message
        self.watch_root = self.tmp_root / "watch"
        self.watch_root.mkdir()
        self.rpc_calls = []
        self.queue = {}
        self.turn = {}
        self.rpc_errors = {}

        def rpc(_path, method, params, **_kw):
            self.rpc_calls.append((method, params["threadId"]))
            if params["threadId"] in self.rpc_errors:
                raise _queue_error(self.rpc_errors[params["threadId"]], ambiguous=False)
            if method == "thread/queue/list":
                return {"data": list(self.queue.get(params["threadId"], []))}
            if method == "thread/turns/list":
                turn = self.turn.get(params["threadId"])
                return {"data": [turn] if turn else []}
            raise AssertionError("status used a write rpc: " + method)
        for patcher in (
                patch_split(peer_steward, "_watch_root", return_value=self.watch_root),
                patch_split(peer_steward, "_watch_entries", return_value=[]),
                mock.patch.object(peer_steward, "_current_session_identity",
                                  return_value=("sender-session", "claude")),
                mock.patch("codex_queue_dispatch.resolve_queue_socket", return_value="fixture-socket"),
                mock.patch("codex_queue_delivery._rpc", side_effect=rpc)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def row(self, body, *, thread="codex-thread", sender=None, harness="codex", receipt=None, state=None,
            created=None):
        recipient = {"harness": harness, "session_id": thread, "name": "child", "pane_id": "w1:pX"}
        _text, ref = self.pm.prepare_peer_message(body, sender or self.sender, recipient, defer=True)
        changes = {}
        if receipt:
            changes["receipt"] = receipt
        if state:
            changes["state"] = state
        if created is not None:
            changes["created"] = created
        if changes:
            with self.pm.pending_lock(ref):
                self.pm._save_pending(dict(self.pm._read_pending(ref), **changes))
        return ref

    def status(self, *flags, **overrides):
        args = SimpleNamespace(watch=overrides.get("watch"), undelivered="--undelivered" in flags,
                               json="--json" in flags)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = peer_steward.cmd_status(args)
        return rc, out.getvalue()

    def test_json_shows_exact_codex_queue_and_turn_facts_without_touching_watches(self):
        ref = self.row("needs a look", receipt="native-queue-accepted", state="queued")
        self.queue["codex-thread"] = [
            {"id": "q1", "clientUserMessageId": "delivery-peer-" + ref},
            {"id": "q2", "input": []}]
        self.turn["codex-thread"] = {"id": "turn-5", "status": "interrupted"}
        rc, out = self.status("--json")
        payload = json.loads(out)
        self.assertEqual((rc, payload["watches"], payload["peer_messages_omitted"]), (0, [], 0))
        entry, = payload["peer_messages"]
        self.assertEqual(entry["ref"], ref)
        self.assertEqual((entry["state"], entry["receipt"], entry["steer"]), ("queued", "native-queue-accepted", True))
        self.assertEqual(entry["to"], {"harness": "codex", "session_id": "codex-thread", "name": "child"})
        self.assertEqual(entry["codex"], {"queue": "present", "turn": "interrupted", "turn_id": "turn-5",
                                          "hearting_items": 1, "other_items": 1, "reason": None})
        self.assertIsInstance(entry["age_s"], int)
        self.assertNotIn("peer_messages_error", payload)

    def test_text_line_and_undelivered_filter(self):
        ref = self.row("needs a look")
        self.turn["codex-thread"] = {"id": "turn-5", "status": "inProgress"}
        for flags in ((), ("--undelivered",)):
            rc, out = self.status(*flags)
            line, = out.splitlines()
            self.assertEqual(rc, 0)
            self.assertRegex(line, r"^peer-message ref=%s to=codex:child state=pending receipt=\S+ "
                                   r"age=\d+s steer=yes queue=absent turn=inProgress turn_id=turn-5 "
                                   r"hearting=0 other=0$" % ref)

    def test_only_the_callers_own_rows_and_never_with_watch(self):
        mine = self.row("mine")
        self.row("theirs", sender={"harness": "claude", "session_id": "someone-else", "name": "x"})
        payload = json.loads(self.status("--json")[1])
        self.assertEqual([entry["ref"] for entry in payload["peer_messages"]], [mine])
        rc, out = self.status("--json", watch="abc")
        self.assertNotIn("peer_messages", json.loads(out))
        self.assertEqual(self.status(watch="abc")[1], "")
        self.assertEqual(len(self.rpc_calls), 2)  # only the first, un-filtered call observed Codex

    def test_observation_failure_is_unknown_and_never_breaks_status(self):
        self.row("needs a look")
        self.rpc_errors["codex-thread"] = "queue-websocket-timeout"
        rc, out = self.status("--json")
        entry, = json.loads(out)["peer_messages"]
        self.assertEqual(rc, 0)
        self.assertEqual((entry["codex"]["queue"], entry["codex"]["reason"]),
                         ("unknown", "queue-websocket-timeout"))
        with mock.patch.object(self.pm, "_pending_rows", side_effect=OSError("disk")):
            rc, out = self.status("--json")
            payload = json.loads(out)
            self.assertEqual((rc, payload["peer_messages"], payload["peer_messages_error"]), (0, [], "OSError"))
            self.assertEqual(self.status()[0], 0)

    # --- the undelivered-receipt reader the Claude prompt sweep shares ---
    def _receipt_entry(self, watch_id):
        return {"watch_id": watch_id, "state": "receipt",
                "recipient": {"harness": "claude", "session_id": "sender-session"}}

    def test_undelivered_watch_rows_read_nothing_past_the_deadline(self):
        entries = [(f"w{index}", {"watch_id": f"w{index}"}) for index in range(3)]
        with mock.patch.object(peer_steward, "_status_entry",
                               side_effect=AssertionError("read past the deadline")):
            self.assertEqual(peer_steward.undelivered_watch_rows(
                entries, self.watch_root, deadline=time.monotonic() - 1), [])

    def test_undelivered_watch_rows_skip_queued_notices_and_hold_unproven_ones(self):
        entries = [("w1", {"watch_id": "w1"}), ("w2", {"watch_id": "w2"})]
        queued = [{"refs": ["peer-watch:w2"]}]

        def entry(watch_id, _arm, _root):
            return self._receipt_entry(watch_id)

        with mock.patch.object(peer_steward, "_status_entry", side_effect=entry), \
                mock.patch.object(self.pm, "_pending_rows", return_value=iter(queued)):
            rows = peer_steward.undelivered_watch_rows(entries, self.watch_root)
        self.assertEqual([row["watch_id"] for row in rows], ["w1"])

        def slow_queue(**_kwargs):
            time.sleep(0.3)
            return iter(())

        # The queued-notice read ran out of time: listing w1/w2 now could show a
        # receipt its own notice also carries, so the prompt shows none of them.
        with mock.patch.object(peer_steward, "_status_entry", side_effect=entry), \
                mock.patch.object(self.pm, "_pending_rows", side_effect=slow_queue):
            self.assertEqual(peer_steward.undelivered_watch_rows(
                entries, self.watch_root, deadline=time.monotonic() + 0.2), [])

    def test_status_reads_but_never_writes_the_native_queue(self):
        for index in range(3):
            self.row("note %d" % index, thread="thread-%d" % index)
        self.status("--json")
        self.assertTrue(self.rpc_calls)
        self.assertLessEqual({method for method, _thread in self.rpc_calls},
                             {"thread/queue/list", "thread/turns/list"})

    def test_observation_and_row_budgets(self):
        for index in range(peer_steward._PEER_STATUS_MAX_THREADS + 1):
            self.row("thread note %d" % index, thread="thread-%d" % index, created=1000.0 + index)
        entries = json.loads(self.status("--json")[1])["peer_messages"]
        self.assertEqual([entry["codex"]["reason"] for entry in entries].count("observation-cap"), 1)
        self.assertEqual(entries[-1]["codex"]["reason"], "observation-cap")
        self.assertEqual(len({thread for _method, thread in self.rpc_calls}), peer_steward._PEER_STATUS_MAX_THREADS)
        self.assertEqual(peer_steward._PEER_STATUS_RPC_BUDGET, 1.0)
        self.assertEqual(peer_steward._PEER_STATUS_RPC_TIMEOUT, 0.25)
        for index in range(25):
            self.row("bulk %d" % index, thread="bulk-thread", created=5000.0 + index)
        payload = json.loads(self.status("--json")[1])
        self.assertEqual(len(payload["peer_messages"]), peer_steward._PEER_STATUS_MAX_ROWS)
        self.assertEqual(payload["peer_messages_omitted"], peer_steward._PEER_STATUS_MAX_THREADS + 1 + 25 - 20)
        self.assertIn("peer-message omitted=", self.status()[1])

    def test_non_codex_rows_have_no_codex_block(self):
        self.row("to claude", thread="claude-session", harness="claude")
        entry, = json.loads(self.status("--json")[1])["peer_messages"]
        self.assertNotIn("codex", entry)
        self.assertFalse(entry["steer"])
        self.assertEqual(self.rpc_calls, [])


class UndeliveredWatchSweepReadsTest(_TmpRootMixin, unittest.TestCase):
    """HOOK-LATENCY: the prompt sweep reads what can be this session's, not every watch.

    Counted reads, not timing: a seat ledger read once per watch made the sweep
    cost O(all watches of all sessions), so under load its one-second share
    ran out and the fallback carrier showed nothing.
    """

    def setUp(self):
        super().setUp()
        import dispatch_seat_handover as handover
        import session_tidy
        self.handover, self.tidy = handover, session_tidy
        self.primary = peer_steward.peer_message.peer_state_root()
        self.root = peer_steward._watch_root()
        self.root.mkdir(parents=True, exist_ok=True)
        self.sessions = session_tidy.state_root() / "sessions"
        self.sessions.mkdir(parents=True, exist_ok=True)
        identity = mock.patch.object(peer_steward, "_current_session_identity",
                                     return_value=("me", "claude"))
        identity.start()
        self.addCleanup(identity.stop)

    def _watch(self, watch_id, harness, sid, seat):
        arm = {"watch_id": watch_id, "target": "target-" + watch_id, "server": "default",
               "until": [], "agent": {"harness": "codex", "pane": "w1:p" + watch_id},
               "armed_ts": "then", "deadline": None, "wake": "none",
               "steward": {"harness": harness, "session_id": sid},
               "steward_seat": {"kind": "pane", "key": seat},
               "watcher": {"pid": 0, "pid_start": "0"}}
        (self.root / f"{watch_id}.json").write_text(json.dumps(arm), encoding="utf-8")
        (self.root / f"{watch_id}.receipt.json").write_text(json.dumps(
            {"watch_id": watch_id, "target": arm["target"], "steward": arm["steward"]}),
            encoding="utf-8")
        return arm

    def _hand_over(self, seat, arm, successor):
        relation = self.handover._watch_reference_from_arm(arm["watch_id"], self.primary, arm)
        relation.update(holder={"harness": "claude", "sid": successor},
                        lineage=[{"from": arm["steward"]["session_id"], "to": successor,
                                  "snapshot_id": "fixture-" + arm["watch_id"]}])
        with (self.sessions / f"{seat}.jsonl").open("a", encoding="utf-8") as ledger:
            ledger.write(json.dumps({
                "ts": 1.0, "harness": "claude", "sid": successor, "event": "handover",
                "from": arm["steward"]["session_id"], "seat_key": seat, "bindings": [],
                "steward": {"schema": 1, "roles": [], "watches": [relation]}}) + "\n")

    def _unrelated(self, start, count):
        # Other harnesses, other same-harness sessions on two seats, and the
        # predecessor's own watches it kept or gave to someone else.
        for index in range(start, start + count):
            kind = index % 4
            if kind == 0:
                self._watch(f"u{index}", "codex", f"codex-{index}", "seat-codex")
            elif kind in {1, 2}:
                self._watch(f"u{index}", "claude", f"other-{index}", f"seat-{kind}")
            else:
                arm = self._watch(f"u{index}", "claude", "pred", "seat-pred")
                if index % 8 == 3:
                    self._hand_over("seat-pred", arm, "someone-else")

    def _counted_sweep(self):
        reads = []
        tidy_read, steward_read = self.tidy.read_bytes, peer_steward._read_json

        def read_bytes(path):
            reads.append(Path(path))
            return tidy_read(path)

        def read_json(path):
            reads.append(Path(path))
            return steward_read(path)

        with mock.patch.object(self.tidy, "read_bytes", side_effect=read_bytes), \
                patch_split(peer_steward, "_read_json", side_effect=read_json):
            rows = peer_steward.undelivered_watch_rows()
        ledgers = sum(1 for path in reads if path.suffix == ".jsonl")
        watch_files = sum(1 for path in reads if path.parent == self.root)
        return sorted(row["watch_id"] for row in rows), ledgers, watch_files

    def test_reads_do_not_grow_with_other_sessions_watches(self):
        self._watch("mine", "claude", "me", "seat-me")
        self._hand_over("seat-pred", self._watch("inherited", "claude", "pred", "seat-pred"), "me")
        self._unrelated(0, 40)
        learn = self._counted_sweep()  # the first call reads each arm once and keeps its steward
        forty = self._counted_sweep()
        self._unrelated(40, 40)
        new_arms = self._counted_sweep()
        eighty = self._counted_sweep()
        for result in (learn, forty, new_arms, eighty):
            self.assertEqual(result[0], ["inherited", "mine"])
        # This session's two arms plus their ten status files. Base: every arm
        # on every call, 52 and 92 watch-file reads.
        self.assertEqual(forty[1:], eighty[1:], {"40": forty, "80": eighty})
        self.assertEqual(forty[1:], (4, 12), forty)
        self.assertEqual(learn[2] - forty[2], 40, learn)
        self.assertEqual(new_arms[2] - forty[2], 40, new_arms)

    def test_an_unreadable_or_missing_index_reads_the_arms_again(self):
        self._watch("mine", "claude", "me", "seat-me")
        self._unrelated(0, 8)
        index = self.root / ".steward-index.json"
        for damage in ("missing", "torn", "wrong-schema"):
            with self.subTest(damage=damage):
                self._counted_sweep()
                if damage == "missing":
                    index.unlink()
                elif damage == "torn":
                    index.write_text('{"schema": 1, "watches": {', encoding="utf-8")
                else:
                    index.write_text('{"schema": 2, "watches": {}}', encoding="utf-8")
                rows, _ledgers, reads = self._counted_sweep()
                self.assertEqual(rows, ["mine"])
                self.assertEqual(reads - self._counted_sweep()[2], 8)
                self.assertEqual(json.loads(index.read_text(encoding="utf-8"))["schema"], 1)

    def test_index_keeps_only_the_steward_and_a_new_receipt_is_found(self):
        self._unrelated(0, 4)
        self._counted_sweep()
        # An arm armed earlier whose receipt lands after the index was written.
        arm = self._watch("late", "claude", "me", "seat-me")
        receipt = self.root / "late.receipt.json"
        receipt.unlink()
        self.assertEqual(self._counted_sweep()[0], [])
        receipt.write_text(json.dumps({"watch_id": "late", "steward": arm["steward"]}),
                           encoding="utf-8")
        self.assertEqual(self._counted_sweep()[0], ["late"])
        stored = json.loads((self.root / ".steward-index.json").read_text(encoding="utf-8"))
        self.assertEqual(stored["watches"]["late"], ["claude", "me", "seat-me"])
        # A receipt whose arm was never written still describes itself.
        (self.root / "orphan.receipt.json").write_text(json.dumps(
            {"watch_id": "orphan", "steward": {"harness": "claude", "session_id": "me"}}),
            encoding="utf-8")
        self.assertEqual(self._counted_sweep()[0], ["late", "orphan"])


class ClearTest(_TmpRootMixin, unittest.TestCase):

    def setUp(self):
        super().setUp()
        import session_tidy as st
        import session_tidy_clear as clear
        self.st, self.clear = st, clear
        self.seat = st.Seat("pane", st._digest("pane", "w1:pX"), "w1:pX", "claude", "")
        self.path = clear.reservation_path(self.seat.key)
        self.claude_transcript("sid-A")

    def claude_transcript(self, sid):
        """A Claude session that never launched a background agent (no subagents folder)."""
        path = Path(os.environ["HOME"]) / ".claude" / "projects" / "-proj" / f"{sid}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_text(json.dumps({"type": "user", "sessionId": sid}) + "\n", encoding="utf-8")
        return path

    def book(self, harness="claude", sid="sid-A", pane="w1:pX", seq=0, deadline_in=600):
        st, clear = self.st, self.clear
        if harness == "claude":
            self.claude_transcript(sid)
        seat = st.Seat("pane", st._digest("pane", pane), pane, harness, "")
        now = time.time()
        with st.seat_lock(seat.key):
            card = st.write_card(seat, harness, sid, "card body", prompt_seq=seq)
            clear._write_reservation({
                "schema": 1, "nonce": "n0nce", "status": "reserved", "created": now, "deadline": now + deadline_in,
                "seat": {"kind": "pane", "key": seat.key, "pane": pane, "harness": harness, "project_key": ""},
                "harness": harness, "sid": sid, "cwd": str(self.tmp_root),
                "card_generation": card["generation"], "prompt_seq": seq})
        self.path = clear.reservation_path(seat.key)
        return seat

    def clear_cmd(self, world, nonce="n0nce", extra=()):
        argv = ["clear", world.pane, "--request", str(self.path), *( ["--nonce", nonce] if nonce else [] ), *extra]
        printed = []
        with mock.patch.object(peer_steward.shutil, "which", return_value="/usr/bin/herdr"), \
             mock.patch.object(peer_steward.subprocess, "run", side_effect=world.run), \
             mock.patch("builtins.print", side_effect=lambda *a, **k: printed.append(" ".join(map(str, a)))):
            rc = peer_steward.main(argv)
        return rc, (printed[-1] if printed else "")

    def rows(self):
        return [r for r in self._all_records() if r.get("kind") == "notice"]

    # -- success ------------------------------------------------------------------------

    def test_an_idle_claude_with_an_empty_box_gets_exactly_one_clear_and_it_is_true(self):
        self.book()
        world = _ClearWorld(screens=[CLAUDE_EMPTY])
        rc, line = self.clear_cmd(world)
        self.assertEqual(rc, 0, line)
        self.assertIn("cleared=true", line)
        self.assertIn("old_session=sid-A", line)
        self.assertIn("new_session=sid-B", line)
        prompts = [c for c in world.calls if c[:3] == ["herdr", "agent", "prompt"]]
        self.assertEqual(prompts, [["herdr", "agent", "prompt", "w1:pX", "/clear"]])   # no trailer, no wait
        self.assertEqual([c for c in world.calls if c[:3] == ["herdr", "agent", "send-keys"]], [])

    def test_background_agents_get_one_compact_and_the_helper_waits_for_its_evidence(self):
        self.book()
        transcript = self.claude_transcript("sid-A")
        agents = transcript.with_suffix("") / "subagents"
        agents.mkdir(parents=True)
        (agents / "agent-a1.meta.json").write_text(json.dumps({"requestShape": "background"}), encoding="utf-8")
        world = _ClearWorld(screens=[CLAUDE_EMPTY])
        rc, line = self.clear_cmd(world)
        self.assertEqual(rc, 5, line)
        self.assertIn("cleared=unverified", line)
        self.assertIn("reason=compact-pending", line)
        prompts = [c for c in world.calls if c[:3] == ["herdr", "agent", "prompt"]]
        self.assertEqual(prompts, [["herdr", "agent", "prompt", "w1:pX", "/compact"]])
        claim = self.clear.read_reservation(self.seat.key)["clear_claim"]
        self.assertEqual((claim["command"], claim["transcript"], claim["offset"]),
                         ("compact", str(transcript), transcript.stat().st_size))

    def test_a_compact_booking_types_only_after_the_turn_ended_and_only_once(self):
        st, clear = self.st, self.clear
        transcript = self.tmp_root / "sid-A.jsonl"
        transcript.write_text(json.dumps({"type": "user", "uuid": "u-tool", "message": {
            "content": [{"type": "tool_result", "tool_use_id": "t", "content": "ok"}]}}) + "\n", encoding="utf-8")
        info = transcript.stat()
        context = {"trigger": "auto", "identity": "steward-compact", "transcript": str(transcript),
                   "boundary_offset": info.st_size, "transcript_identity": f"{info.st_dev}:{info.st_ino}"}
        seat = st.Seat("pane", st._digest("pane", "w1:pX"), "w1:pX", "claude", "")
        now = time.time()
        with st.seat_lock(seat.key):
            card = st.write_card(seat, "claude", "sid-A", "summary", prompt_seq=0, origin="runtime-compact",
                                 compact_context=context)
            clear._write_reservation({
                "schema": 1, "nonce": "n0nce", "status": "reserved", "created": now, "deadline": now + 600,
                "seat": {"kind": "pane", "key": seat.key, "pane": "w1:pX", "harness": "claude", "project_key": ""},
                "harness": "claude", "sid": "sid-A", "cwd": str(self.tmp_root), "origin": "compact",
                "compact": {**context}, "card_generation": card["generation"], "prompt_seq": 0})
        self.path = clear.reservation_path(seat.key)
        world = _ClearWorld(screens=[CLAUDE_EMPTY])
        rc, line = self.clear_cmd(world)
        self.assertEqual(rc, 3, line)
        self.assertIn("reason=compact-pending", line)
        self.assertEqual(world.typed(), [])
        with transcript.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"type": "assistant", "uuid": "a-end", "message": {
                "stop_reason": "end_turn", "content": [{"type": "text", "text": "done"}]}}) + "\n")
        rc, line = self.clear_cmd(world)
        self.assertEqual(rc, 0, line)
        self.assertEqual(len(world.typed()), 1)
        rc, line = self.clear_cmd(world)            # the claim was consumed: nothing is typed again
        self.assertEqual(rc, 3, line)
        self.assertEqual(len(world.typed()), 1)

    def test_a_claude_suggestion_is_an_empty_box_but_typed_text_is_not(self):
        for screen, expect in ((CLAUDE_SUGGESTION, "cleared=true"), (CLAUDE_DRAFT, "reason=draft"),
                               (CLAUDE_DRAFT_SECOND_LINE, "reason=draft")):
            with self.subTest(screen=screen[:30]):
                self.book()
                world = _ClearWorld(screens=[screen])
                rc, line = self.clear_cmd(world)
                self.assertIn(expect, line)
                self.assertEqual(rc, 0 if expect == "cleared=true" else 3)
                self.assertEqual(len(world.typed()), 1 if rc == 0 else 0)

    def test_codex_placeholder_is_empty_a_draft_or_a_popup_is_not(self):
        for screen, expect in ((CODEX_EMPTY, "cleared=true"), (CODEX_DRAFT, "reason=draft"),
                               (CODEX_POPUP, "reason=draft-unknown")):
            with self.subTest(screen=screen[:40]):
                self.book(harness="codex", sid="thr-A")
                world = _ClearWorld(harness="codex", sid="thr-A", screens=[screen], new_sid="thr-B")
                # This screen fixture has an explicit current native selection.
                with mock.patch.object(peer_steward, "_process_session", side_effect=
                                       lambda *_: None if world.sent else world.sid):
                    rc, line = self.clear_cmd(world)
                self.assertIn(expect, line)
                self.assertEqual(len(world.typed()), 1 if expect == "cleared=true" else 0)

    def test_opencode_types_new_and_the_home_screen_is_the_proof(self):
        self.book(harness="opencode", sid="ses_A")
        world = _ClearWorld(harness="opencode", sid="ses_A", screens=[OPENCODE_EMPTY], new_sid=None,
                            home_after=OPENCODE_HOME)
        with mock.patch.object(peer_steward, "_process_session",
                               side_effect=lambda *_args: None if world.sent else "ses_A"):
            rc, line = self.clear_cmd(world)
        self.assertEqual((rc, "cleared=true" in line), (0, True), line)
        self.assertEqual(world.typed(), [["herdr", "agent", "prompt", "w1:pX", "/new"]])
        for after, expect in ((OPENCODE_HOME_BLANK, "cleared=true"), (OPENCODE_EMPTY, "cleared=unverified"),
                              ("\n\n", "cleared=unverified")):
            with self.subTest(after=after[:20]):
                self.book(harness="opencode", sid="ses_A")
                world = _ClearWorld(harness="opencode", sid="ses_A", screens=[OPENCODE_EMPTY], new_sid=None,
                                    home_after=after)
                with mock.patch.object(peer_steward, "_process_session",
                                       side_effect=lambda *_args: None if world.sent else "ses_A"), \
                     mock.patch.object(peer_steward, "_CLEAR_OBSERVE_ROUNDS", 1):
                    rc, line = self.clear_cmd(world)
                self.assertIn(expect, line)
                self.assertEqual(len(world.typed()), 1, line)
        # OpenCode's pane stays `done` through /new, so the pause between looks is a wait for the
        # screen (event-driven), not the idle wait that would return at once.
        self.book(harness="opencode", sid="ses_A")
        world = _ClearWorld(harness="opencode", sid="ses_A", screens=[OPENCODE_EMPTY], new_sid=None,
                            home_after=OPENCODE_EMPTY)
        with mock.patch.object(peer_steward, "_process_session",
                               side_effect=lambda *_args: None if world.sent else "ses_A"), \
             mock.patch.object(peer_steward, "_CLEAR_OBSERVE_ROUNDS", 3):
            rc, line = self.clear_cmd(world)
        self.assertIn("cleared=unverified", line)
        self.assertEqual(len([c for c in world.calls if c[:3] == ["herdr", "pane", "wait-output"]]), 2)
        for screen, reason in ((OPENCODE_DRAFT, "draft"), (NO_BOX, "draft-unknown")):
            self.book(harness="opencode", sid="ses_A")
            world = _ClearWorld(harness="opencode", sid="ses_A", screens=[screen], new_sid=None)
            with mock.patch.object(peer_steward, "_process_session",
                                   side_effect=lambda *_args: None if world.sent else "ses_A"):
                rc, line = self.clear_cmd(world)
            self.assertEqual((rc, f"reason={reason}" in line, world.typed()), (3, True, []), line)

    # -- doubt is "not cleared": zero input ---------------------------------------------------

    def test_working_blocked_unreadable_and_form_screens_get_no_input(self):
        cases = (({"status": "working"}, "not-idle-working"), ({"status": "blocked"}, "form-open"),
                 ({"status": "unknown"}, "not-idle-unknown"), ({"screens": [None]}, "screen-unknown"),
                 ({"screens": [CLAUDE_FORM]}, "form-open"), ({"screens": [NO_BOX]}, "draft-unknown"))
        for kwargs, reason in cases:
            with self.subTest(reason=reason):
                self.book()
                world = _ClearWorld(**kwargs)
                rc, line = self.clear_cmd(world)
                self.assertEqual(rc, 3, line)
                self.assertIn(f"reason={reason}", line)
                self.assertEqual(world.typed(), [])

    def test_a_draft_that_appears_between_the_two_looks_is_never_typed_over(self):
        self.book()
        world = _ClearWorld(screens=[CLAUDE_EMPTY, CLAUDE_DRAFT])      # the user starts typing after look 1
        rc, line = self.clear_cmd(world)
        self.assertEqual((rc, "reason=draft" in line, world.typed()), (3, True, []), line)

    def test_a_new_request_expiry_a_stale_nonce_or_a_finished_booking_get_no_input(self):
        seat = self.book(seq=0)
        with self.st.seat_lock(seat.key):
            self.st.bump_prompt_seq(seat, "claude", "sid-A", time.time())
        world = _ClearWorld()
        rc, line = self.clear_cmd(world)
        self.assertEqual((rc, "reason=new-input" in line), (3, True))
        self.book(deadline_in=-5)
        rc, line = self.clear_cmd(world)
        self.assertEqual((rc, "reason=expired" in line), (3, True))
        self.book()
        rc, line = self.clear_cmd(world, nonce="another")
        self.assertEqual((rc, "reason=superseded" in line), (3, True))
        self.assertEqual(world.typed(), [])
        self.assertEqual(self.rows(), [])                               # a dead request leaves no ledger row

    def test_the_target_must_be_the_booked_pane_harness_and_session(self):
        for kwargs, label in (({"harness": "codex"}, "harness"), ({"sid": "someone-else"}, "session")):
            with self.subTest(label):
                self.book()
                world = _ClearWorld(**kwargs)
                rc, line = self.clear_cmd(world)
                self.assertEqual((rc, "reason=target-changed" in line, world.typed()), (3, True, []), line)
        self.book()
        world = _ClearWorld()
        world.pane = "w9:pZ"
        rc, line = self.clear_cmd(world)
        self.assertEqual((rc, "reason=target-changed" in line), (3, True))

    def test_the_new_session_is_the_process_one_when_herdr_reports_a_stale_session(self):
        for proof, expect in (("sid-NEW", "cleared=true"), ("sid-A", "cleared=unverified")):
            with self.subTest(process_says=proof):
                self.book()
                world = _ClearWorld(sid="sid-A", new_sid="sid-stale-older")   # herdr names an older session
                with mock.patch("fleet.collectors.claude.session_id_of_process", return_value=proof), \
                        mock.patch.object(peer_steward, "_CLEAR_OBSERVE_ROUNDS", 1):
                    rc, line = self.clear_cmd(world)
                self.assertIn(expect, line)
                if expect == "cleared=true":
                    self.assertIn("new_session=sid-NEW", line)

    def test_a_pane_record_that_lags_a_clear_is_settled_by_the_process_not_guessed(self):
        # herdr still reports the session before the previous /clear; the process knows better.
        for proof, expect_typed in (("sid-A", 1), ("sid-other", 0), (None, 0)):
            with self.subTest(process_says=proof):
                self.book()
                world = _ClearWorld(sid="sid-before-the-last-clear", new_sid="sid-B")
                with mock.patch("fleet.collectors.claude.session_id_of_process",
                                side_effect=lambda pid: "sid-B" if world.sent else proof):
                    rc, line = self.clear_cmd(world)
                self.assertEqual(len(world.typed()), expect_typed, line)
                self.assertEqual("cleared=true" in line, bool(expect_typed), line)
                if not expect_typed:
                    self.assertIn("reason=target-changed", line)

    def test_a_pane_record_without_a_session_id_is_cleared_only_when_the_process_names_the_booked_one(self):
        # herdr names no session (`-`): only a current native pane proof can vouch for it.
        for harness, collector in (("claude", "claude"), ("codex", "codex")):
            for proof, expect_typed in (("sid-A", 1), ("sid-other", 0), (None, 0)):
                with self.subTest(harness=harness, process_says=proof):
                    self.book(harness=harness)
                    screen = CLAUDE_EMPTY if harness == "claude" else CODEX_EMPTY
                    world = _ClearWorld(harness=harness, sid=None, new_sid=None, screens=[screen])
                    with mock.patch(f"fleet.collectors.{collector}.session_id_of_process", return_value=proof), \
                            mock.patch("pane_ownership.current_codex_thread",
                                       return_value=proof or ""), \
                            mock.patch.object(peer_steward, "_CLEAR_OBSERVE_ROUNDS", 1):
                        rc, line = self.clear_cmd(world)
                    self.assertEqual(len(world.typed()), expect_typed, line)
                    if not expect_typed:
                        reason = "session-identity-unknown" if harness == "codex" and proof is None else "target-changed"
                        self.assertEqual((rc, f"reason={reason}" in line), (3, True), line)

    def test_a_codex_seat_ledger_cannot_replace_the_current_native_selection(self):
        st = self.st
        seat = st.Seat("pane", st._digest("pane", "w1:pX"), "w1:pX", "codex", "")
        with st.seat_lock(seat.key):
            st.record_event(seat, "codex", "sid-A", "prompt", cwd="/w", now=time.time() - 100)
            st.record_event(seat, "codex", "sid-B", "prompt", cwd="/w", now=time.time() - 50)
        for metadata, native in (("sid-A", "sid-B"), ("sid-B", "sid-A"), ("sid-B", None)):
            with self.subTest(metadata=metadata, native=native):
                self.book(harness="codex", sid="sid-B")
                world = _ClearWorld(harness="codex", sid=metadata, new_sid=None, screens=[CODEX_EMPTY])
                with mock.patch.object(peer_steward, "_process_session", return_value=native), \
                     mock.patch.object(peer_steward, "_CLEAR_OBSERVE_ROUNDS", 1):
                    rc, line = self.clear_cmd(world)
                self.assertEqual(len(world.typed()), int(native == "sid-B"), line)
                if native == "sid-B":
                    self.assertIn("cleared=unverified", line)
                else:
                    reason = "target-changed" if native else "session-identity-unknown"
                    self.assertIn("reason=" + reason, line)


    def test_codex_native_judgment_is_taken_once_per_look_with_lagging_metadata(self):
        for final in ("sid-B", None, "sid-C"):
            with self.subTest(final_native=final):
                st = self.st
                seat = st.Seat("pane", st._digest("pane", "w1:pX"), "w1:pX", "codex", "")
                with st.seat_lock(seat.key):
                    st.record_event(seat, "codex", "sid-A", "prompt", cwd="/w", now=time.time() - 100)
                    st.record_event(seat, "codex", "sid-B", "prompt", cwd="/w", now=time.time() - 50)
                self.book(harness="codex", sid="sid-B")
                world = _ClearWorld(harness="codex", sid="sid-A", new_sid=None, screens=[CODEX_EMPTY])
                original_look = peer_steward._clear_look
                look_number, reads = 0, []

                def look(*args):
                    nonlocal look_number
                    look_number += 1
                    return original_look(*args)

                def native(*args):
                    value = "sid-B" if look_number == 1 else final
                    if not world.sent:
                        reads.append((look_number, value))
                    return value

                with mock.patch.object(peer_steward, "_clear_look", side_effect=look), \
                     mock.patch.object(peer_steward, "_process_session", side_effect=native), \
                     mock.patch.object(peer_steward, "_CLEAR_OBSERVE_ROUNDS", 1):
                    rc, line = self.clear_cmd(world)
                self.assertEqual(len(world.typed()), int(final == "sid-B"), line)
                if final != "sid-B":
                    reason = "session-identity-unknown" if final is None else "target-changed"
                    self.assertIn("reason=" + reason, line)
                # Bind the change to the final input look, not to a fourth
                # reader call that disappears when redundant reads are removed.
                self.assertEqual(reads, [(1, "sid-B"), (2, final)])

    # -- after the send: never a second one -------------------------------------------------

    def test_a_send_that_cannot_be_confirmed_is_unverified_and_nothing_else_is_typed(self):
        self.book()
        world = _ClearWorld(new_sid=None)                               # herdr keeps the old id, no hook note
        rc, line = self.clear_cmd(world)
        self.assertEqual(rc, 5, line)
        self.assertIn("cleared=unverified", line)
        self.assertEqual(len(world.typed()), 1)                         # one /clear, no Enter retry, no resend

    def test_the_start_hook_note_alone_proves_the_new_conversation(self):
        seat = self.book()
        world = _ClearWorld(new_sid=None)
        original = world.run

        def run(argv, **kw):
            result = original(argv, **kw)
            if argv[:3] == ["herdr", "agent", "prompt"]:               # the new session's start hook fires
                with self.st.seat_lock(seat.key):
                    self.clear.note_start_locked(seat, "claude", "sid-NEW", time.time() + 1)
            return result
        world.run = run
        rc, line = self.clear_cmd(world)
        self.assertEqual((rc, "new_session=sid-NEW" in line), (0, True), line)

    def test_a_failing_herdr_prompt_is_failed_and_herdr_missing_is_failed(self):
        self.book()
        world = _ClearWorld()
        world.prompt_rc = 1
        rc, line = self.clear_cmd(world)
        self.assertEqual((rc, "cleared=failed" in line), (1, True), line)
        printed = []
        with mock.patch.object(peer_steward.shutil, "which", return_value=None), \
             mock.patch("builtins.print", side_effect=lambda *a, **k: printed.append(" ".join(map(str, a)))):
            rc = peer_steward.main(["clear", "w1:pX", "--request", str(self.path)])
        self.assertEqual((rc, printed[-1]), (1, "cleared=failed reason=herdr-not-found"))

    def test_a_new_thread_on_the_codex_status_line_proves_the_clear(self):
        self.book(harness="codex", sid=OLD_T)
        world = _ClearWorld(harness="codex", sid=OLD_T, new_sid=None, screens=[codex_screen("proj · main · Fix it")],
                            home_after=codex_screen(f"proj · main · Context 0% used · {NEW_T}"))
        with mock.patch("fleet.collectors.codex.session_id_of_process", return_value=None), \
                mock.patch.object(peer_steward, "_CLEAR_OBSERVE_ROUNDS", 1):
            # This screen fixture has an explicit current native selection.
            with mock.patch.object(peer_steward, "_process_session", side_effect=
                                   lambda *_: None if world.sent else world.sid):
                rc, line = self.clear_cmd(world)
        self.assertEqual((rc, f"new_session={NEW_T}" in line), (0, True), line)
        self.assertEqual(len(world.typed()), 1)

    def test_only_the_complete_last_status_line_field_is_current_identity(self):
        other = "01000014-aaaa-7bbb-8ccc-dddddddddddd"
        st = self.st
        seat = st.Seat("pane", st._digest("pane", "w1:pX"), "w1:pX", "codex", "")
        with st.seat_lock(seat.key):
            st.record_event(seat, "codex", other, "prompt", cwd="/w", now=time.time() - 300)
        cases = ((f"proj · {NEW_T} · Fix it", f"proj · {NEW_T} · Fix it", "already shown", False),
                 ("proj · Fix it", f"proj · {NEW_T} · 01000014-eeee-7fff-8000-111111111111",
                  "earlier UUID does not override last", True),
                 ("proj · Fix it", "proj · Context 0% used · 01000014-d65e-7041-9744-c8f5300f…",
                  "cut id", False),
                 (f"proj · {OLD_T}", f"proj · {OLD_T} · {NEW_T}",
                  "last field wins over earlier UUID", True),
                 ("proj · Fix it", f"proj · {other}", "a session the seat already knows", False))
        for before, after, label, changed in cases:
            with self.subTest(label):
                self.book(harness="codex", sid=OLD_T)
                world = _ClearWorld(harness="codex", sid=OLD_T, new_sid=None, screens=[codex_screen(before)],
                                    home_after=codex_screen(after))
                with mock.patch("fleet.collectors.codex.session_id_of_process", return_value=None), \
                        mock.patch.object(peer_steward, "_CLEAR_OBSERVE_ROUNDS", 1):
                    # This screen fixture has an explicit current native selection.
                    with mock.patch.object(peer_steward, "_process_session", side_effect=
                                           lambda *_: None if world.sent else world.sid):
                        rc, line = self.clear_cmd(world)
                self.assertEqual("cleared=true" in line, changed, line)
                self.assertEqual(rc, 0 if changed else 5, line)

    def test_a_user_opened_codex_location_menu_is_never_selected_by_clear(self):
        self.book(harness="codex", sid=OLD_T)
        menu = ("Where should the new conversation run?\n"
                "❯ Use current Git worktree\n"
                "Keep using the current working directory\n"
                "Create new Git worktree\n")
        world = _ClearWorld(harness="codex", sid=OLD_T, screens=[menu])
        # This screen fixture has an explicit current native selection.
        with mock.patch.object(peer_steward, "_process_session", side_effect=
                               lambda *_: None if world.sent else world.sid):
            rc, line = self.clear_cmd(world)
        self.assertEqual(rc, 3, line)
        self.assertIn("cleared=skipped", line)
        self.assertIn("reason=form-open", line)
        self.assertEqual(world.typed(), [])

    def test_a_clear_opened_location_menu_is_not_reported_as_cleared_or_selected(self):
        self.book(harness="codex", sid=OLD_T)
        menu = ("Where should the new conversation run?\n"
                "❯ Use current Git worktree\n"
                "Keep using the current working directory\n"
                "Create new Git worktree\n")
        world = _ClearWorld(harness="codex", sid=OLD_T, new_sid=NEW_T,
                            screens=[CODEX_EMPTY], home_after=menu)
        with mock.patch.object(peer_steward, "_CLEAR_OBSERVE_ROUNDS", 1):
            # This screen fixture has an explicit current native selection.
            with mock.patch.object(peer_steward, "_process_session", side_effect=
                                   lambda *_: None if world.sent else world.sid):
                rc, line = self.clear_cmd(world)
        self.assertEqual(rc, 5, line)
        self.assertIn("cleared=unverified", line)
        self.assertEqual(len(world.typed()), 1)  # the authorized /clear only
        self.assertEqual([c for c in world.calls if c[:3] == ["herdr", "agent", "send-keys"]], [])

    def test_every_judged_call_leaves_one_notice_ledger_row_with_the_action(self):
        self.book()
        self.clear_cmd(_ClearWorld(screens=[CLAUDE_DRAFT]))
        self.book()
        self.clear_cmd(_ClearWorld(screens=[CLAUDE_EMPTY]))
        rows = self.rows()
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(r["kind"] == "notice" and "action=clear" in r["delivery"]["receipt"] for r in rows))
        self.assertIn("cleared=skipped", rows[0]["delivery"]["receipt"])
        self.assertIn("cleared=true", rows[1]["delivery"]["receipt"])
        self.assertEqual((rows[1]["to"]["pane"], rows[1]["from"]["session_id"]), ("w1:pX", "sid-A"))

    def test_the_screen_reader_decides_from_layout_not_from_wishful_text(self):
        lines = peer_steward._screen_lines
        draft = peer_steward._draft_state
        self.assertEqual(draft("claude", lines(CLAUDE_EMPTY)), "empty")
        self.assertEqual(draft("claude", lines(RULE + "\n❯ typed\n")), "nonempty")   # no closing rule: still a draft
        self.assertEqual(draft("claude", lines(RULE + "\n❯\n")), "unknown")           # box never closes: unknown
        self.assertEqual(draft("codex", lines("›\n")), "empty")
        self.assertEqual(draft("opencode", lines("  ┃  just text\n")), "unknown")      # a single bar line is no box
        self.assertEqual(draft("unheard-of", lines(CLAUDE_EMPTY)), "unknown")


class TidyDeferredPeerTest(_TmpRootMixin, unittest.TestCase):
    """Existing pending refs cross the tidy boundary without a user prompt."""

    def setUp(self):
        super().setUp()
        import session_tidy as st
        import session_tidy_clear as clear
        self.st, self.clear = st, clear
        self.sent = []

    def booking(self, harness):
        sid, pane = "tidy-" + harness, "w1:p-tidy"
        seat = self.st.Seat("pane", self.st._native_session_key(harness, sid), pane,
                            harness, sid=sid)
        transcript = self.tmp_root / (sid + ".jsonl")
        transcript.touch()
        self.st.record_event(seat, harness, sid, "start", transcript=str(transcript),
                             cwd=str(self.tmp_root), source="fixture")
        req = dict(schema=1, nonce="tidy-fixture", status="reserved", origin="idle",
                   harness=harness, sid=sid, seat=seat.as_dict(), prompt_seq=0,
                   created=time.time(), deadline=time.time() + 600, continue_off=True,
                   clear_command="compact", idle_request={"state": "sent", "arrival": {"sid": sid}})
        self.clear._write_reservation(req)
        return seat, req

    def pending(self, harness, sid, pane):
        peer = peer_steward.peer_message
        recipient = dict(harness=harness, session_id=sid, pane_id=pane)
        text, ref = peer.prepare_peer_message("fixture result", dict(harness="claude", session_id="sender"),
                                              recipient, defer=True)
        return peer._read_pending(ref), text, recipient

    def compact_claim(self, req, *, now=None):
        from datetime import datetime, timezone
        now = time.time() if now is None else now
        transcript = self.tmp_root / (req["sid"] + ".jsonl")
        transcript.write_text(json.dumps({"type": "system", "subtype": "compact_boundary",
            "sessionId": req["sid"], "timestamp": datetime.fromtimestamp(now, timezone.utc).isoformat()}) + "\n")
        return dict(state="sending", command="compact", at=now - 1,
                    transcript=str(transcript), offset=0)

    @contextlib.contextmanager
    def transport(self, recipient, text, *, state="working"):
        peer = peer_steward.peer_message
        harness, sid, pane = (recipient[k] for k in ("harness", "session_id", "pane_id"))
        ident = dict(harness=harness, session_id=sid, pane=pane, name=pane)
        current = dict(state=state, _identity=ident, _native_session_id=sid, session_id=sid)
        proof = dict(pid=42, start="12", session_id=sid)

        def received():
            self.sent.append(text)
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(peer.receive_peer_message(text, recipient), 0)

        def pty(*args, **kwargs):
            received()
            return 0, {"result": {}}

        def queue(*args, **kwargs):
            if kwargs.get("before_add"):
                kwargs["before_add"]()
            received()
            return {"status": "received"}

        def native(ident, **kwargs):
            if ident.get("harness") != "opencode":
                return None
            book = self.clear.read_reservation(self.st._native_session_key(harness, sid))
            return {"status": "idle" if (book or {}).get("status") == "compacted" else state}

        with contextlib.ExitStack() as stack:
            for name, value in {
                "_retire_target": (state, ident, 0, None),
                "_prompt_current_candidate": (current, None),
                "_prompt_exact_candidate": (current, None),
                "_agent_state": (state, pane),
                "_pane_readiness": SimpleNamespace(state="ready", reason=""),
                "_prompt_foreground_proof": proof,
                "_tmux_input_refusal": None, "_read_screen": ["> "], "_screen_ready": None,
            }.items():
                stack.enter_context(mock.patch.object(peer_steward, name, return_value=value))
            stack.enter_context(mock.patch.object(peer_steward, "_herdr_prompt", side_effect=pty))
            stack.enter_context(mock.patch.object(peer_steward, "_native_opencode_turn", side_effect=native))
            stack.enter_context(mock.patch("codex_queue_dispatch.resolve_queue_socket", return_value="fixture.sock"))
            stack.enter_context(mock.patch("codex_queue_delivery.send_at_least_once", side_effect=queue))
            yield

    def submit(self, row, recipient, state):
        if recipient["harness"] == "codex":
            return peer_steward.peer_message.deliver_pending_codex(row["ref"], timeout=.01)
        return peer_steward._submit_pending_native(row, recipient["pane_id"], recipient["session_id"],
                    recipient["pane_id"], state, harness=recipient["harness"])

    def test_card_turn_peer_is_delivered_after_compact_without_user_input(self):
        for harness in ("claude", "codex", "opencode"):
            with self.subTest(harness=harness):
                self.sent.clear()
                seat, req = self.booking(harness)
                row, text, recipient = self.pending(harness, req["sid"], seat.pane)
                duty = peer_steward._schedule_message_obligation(row, seat.pane, seat.pane)
                store = peer_steward.peer_obligations.ObligationStore(self.tmp_root)
                with self.transport(recipient, text):
                    self.submit(row, recipient, "working")
                    self.assertEqual(self.sent, [])
                    req.update(idle_request={"state": "accepted"},
                               clear_claim=self.compact_claim(req))
                    self.clear._write_reservation(req)
                    with mock.patch("shared_readiness.wake_runner") as wake, \
                         mock.patch.object(self.st, "resolve_seat", return_value=seat), \
                         mock.patch.object(self.clear, "compact_confirmed", return_value=True):
                        self.assertEqual(self.st.run_hook(harness, "compact", req["sid"], phase="after",
                            cwd=str(self.tmp_root), transcript=str(self.tmp_root / (req["sid"] + ".jsonl")),
                            now=time.time()), "")
                        self.assertEqual(self.clear.read_reservation(seat.key)["status"], "compacted")
                        wake.assert_called_once_with(store.root)
                    # Herdr can still say working. The exact completed booking
                    # and unchanged prompt frontier corroborate idle, while the
                    # existing identity/screen checks and carrier remain real.
                    peer_steward._resume_message_obligation(store.get(duty["id"]), store)
                    self.assertEqual(self.sent, [text])
                    self.assertEqual(store.get(duty["id"])["state"], "complete")
                    peer_steward._resume_message_obligation(store.get(duty["id"]), store)
                    self.assertEqual(self.sent, [text])

    def test_completed_booking_releases_stale_working_but_keeps_draft_and_identity_checks(self):
        for harness in ("claude", "opencode"):
            with self.subTest(harness=harness):
                self.sent.clear()
                seat, req = self.booking(harness)
                req.update(status="compacted", finished=time.time(), clear_claim=self.compact_claim(req))
                self.clear._write_reservation(req)
                row, text, recipient = self.pending(harness, req["sid"], seat.pane)
                with self.transport(recipient, text):
                    for name, value in (("_screen_ready", "draft-present"),
                                        ("_prompt_foreground_proof", None)):
                        with mock.patch.object(peer_steward, name, return_value=value):
                            self.submit(row, recipient, "working")
                        self.assertEqual(self.sent, [])
                    self.submit(row, recipient, "working")
                    self.assertEqual(self.sent, [text])

    def test_compact_claim_defers_all_three_existing_carriers(self):
        for harness in ("claude", "codex", "opencode"):
            with self.subTest(harness=harness):
                self.sent.clear()
                seat, req = self.booking(harness)
                req["idle_request"] = {"state": "accepted"}
                self.clear._write_reservation(req)
                path = self.clear.reservation_path(seat.key)
                with mock.patch.object(self.clear, "validate_request",
                                       side_effect=lambda *a, **k: (self.clear.read_reservation(seat.key), "")):
                    claimed, why = self.clear.claim_clear(path, req["nonce"])
                self.assertEqual(why, "")
                self.assertEqual(claimed["clear_claim"]["command"], "compact")
                row, text, recipient = self.pending(harness, req["sid"], seat.pane)
                with self.transport(recipient, text, state="idle"):
                    self.submit(row, recipient, "idle")
                self.assertEqual(self.sent, [])
                held = peer_steward.peer_message._read_pending(row["ref"])
                self.assertEqual((held["state"], held.get("rpc_claim")), ("pending", None))

    def test_peer_admitted_first_keeps_compact_from_writing_over_it(self):
        for harness in ("claude", "codex", "opencode"):
            with self.subTest(harness=harness):
                seat, req = self.booking(harness)
                req["idle_request"] = {"state": "accepted"}
                self.clear._write_reservation(req)
                row, _text, recipient = self.pending(harness, req["sid"], seat.pane)
                claim = peer_steward.peer_message.reserve_pending_herdr(row["ref"], recipient)
                self.assertIsNotNone(claim)
                with mock.patch.object(self.clear, "validate_request",
                                       side_effect=lambda *a, **k: (self.clear.read_reservation(seat.key), "")):
                    result, why = self.clear.claim_clear(self.clear.reservation_path(seat.key), req["nonce"])
                self.assertIsNone(result)
                self.assertEqual(why, "peer-input-pending")

    def test_peer_rechecks_booking_after_compact_wins_the_shared_lock(self):
        seat, req = self.booking("claude")
        req["idle_request"] = {"state": "accepted"}
        self.clear._write_reservation(req)
        row, _text, recipient = self.pending("claude", req["sid"], seat.pane)
        read, proceed = threading.Event(), threading.Event()
        original = self.clear.read_reservation
        owner = threading.get_ident()

        def observe(key):
            result = original(key)
            if threading.get_ident() != owner and not read.is_set():
                read.set()
                if not proceed.wait(3):
                    raise AssertionError("compact claim did not release its input slot")
            return result

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor, \
             mock.patch.object(self.clear, "read_reservation", side_effect=observe):
            future = None
            def validated(*args, **kwargs):
                nonlocal future
                future = executor.submit(peer_steward.peer_message.reserve_pending_herdr,
                                         row["ref"], recipient)
                self.assertTrue(read.wait(2), "peer did not observe the pre-claim booking")
                return original(seat.key), ""
            try:
                with mock.patch.object(self.clear, "validate_request", side_effect=validated):
                    claim, why = self.clear.claim_clear(self.clear.reservation_path(seat.key), req["nonce"])
                self.assertEqual(why, "")
                self.assertEqual(claim["clear_claim"]["command"], "compact")
            finally:
                proceed.set()
            self.assertIsNone(future.result(timeout=3))

    def test_delivery_without_tidy_and_new_input_after_compact_keep_normal_rules(self):
        for harness in ("claude", "codex", "opencode"):
            with self.subTest(harness=harness):
                self.sent.clear()
                row, text, recipient = self.pending(harness, "ordinary-" + harness, "w1:p-normal")
                with self.transport(recipient, text, state="idle"):
                    self.submit(row, recipient, "idle")
                self.assertEqual(self.sent, [text])
        seat, req = self.booking("claude")
        req.update(status="compacted", finished=time.time(), clear_claim={"command": "compact"})
        self.clear._write_reservation(req)
        row, text, recipient = self.pending("claude", req["sid"], seat.pane)
        self.sent.clear()
        with self.transport(recipient, text), mock.patch.object(self.st, "read_prompt_seq", return_value=1):
            self.submit(row, recipient, "working")
        self.assertEqual(self.sent, [])

    def test_native_background_turn_after_compact_is_not_a_stale_working_label(self):
        seat, req = self.booking("claude")
        req.update(status="compacted", finished=time.time(), clear_claim=self.compact_claim(req))
        self.clear._write_reservation(req)
        transcript = self.tmp_root / (req["sid"] + ".jsonl")
        with transcript.open("a") as stream:
            stream.write(json.dumps({"type": "user",
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 1)),
                "message": {"role": "user", "content": "<task-notification>fixture</task-notification>"}}) + "\n")
        row, text, recipient = self.pending("claude", req["sid"], seat.pane)
        with self.transport(recipient, text):
            self.submit(row, recipient, "working")
        self.assertEqual(self.sent, [])

    def test_native_turn_before_delayed_compact_completion_remains_busy(self):
        from datetime import datetime, timezone
        for active in (False, True):
            with self.subTest(new_native_turn=active):
                self.sent.clear()
                seat, req = self.booking("claude")
                base = time.time() - 20
                claim = self.compact_claim(req, now=base + 1)
                stamp = lambda offset: datetime.fromtimestamp(base + offset, timezone.utc).isoformat()
                rows = [
                    {"type": "user", "timestamp": stamp(-1),
                     "message": {"role": "user", "content": "retained old history"}},
                    {"type": "user", "timestamp": stamp(2), "isCompactSummary": True,
                     "message": {"role": "user", "content": "preserved summary"}},
                    {"type": "user", "timestamp": stamp(2), "repeatsCompactSummary": True,
                     "message": {"role": "user", "content": "repeated summary"}},
                    {"type": "user", "timestamp": stamp(2),
                     "message": {"role": "user", "content": "<local-command-stdout>Compacted</local-command-stdout>"}},
                ]
                if active:
                    rows.extend([
                        {"type": "user", "timestamp": stamp(2), "message": {"role": "user",
                         "content": "<task-notification>new native turn</task-notification>"}},
                        {"type": "assistant", "timestamp": stamp(2.5),
                         "message": {"role": "assistant", "content": [{"type": "text", "text": "working"}]}},
                    ])
                with open(claim["transcript"], "a") as stream:
                    for row in rows:
                        stream.write(json.dumps(row) + "\n")
                card = self.st.write_card(seat, "claude", req["sid"], "saved card", prompt_seq=0)
                req.update(card_generation=card["generation"], idle_request={"state": "accepted"},
                           clear_claim=claim)
                self.clear._write_reservation(req)
                with mock.patch.object(self.st, "now_epoch", return_value=base + 3):
                    self.assertEqual(self.clear._await_idle_compaction(seat.key, req["nonce"],
                        sleep=lambda _: self.fail("unexpected wait")), "compacted")
                self.assertEqual(self.st.read_prompt_seq(seat), 0)
                row, text, recipient = self.pending("claude", req["sid"], seat.pane)
                screen_guard = peer_steward._screen_ready
                with self.transport(recipient, text), \
                     mock.patch.object(peer_steward, "_screen_ready", side_effect=screen_guard), \
                     mock.patch.object(peer_steward, "_read_screen",
                                       return_value=peer_steward._screen_lines(CLAUDE_EMPTY)):
                    self.submit(row, recipient, "working")
                self.assertEqual(self.sent, [] if active else [text])

    def test_completed_booking_without_a_current_native_boundary_keeps_working(self):
        for boundary in ("missing", "before-claim", "other-session"):
            with self.subTest(boundary=boundary):
                seat, req = self.booking("claude")
                claim = self.compact_claim(req, now=time.time() - 5)
                transcript = Path(claim["transcript"])
                if boundary == "missing":
                    transcript.write_text("")
                elif boundary == "before-claim":
                    claim["at"] = time.time()
                else:
                    row = json.loads(transcript.read_text())
                    row["sessionId"] = "another-session"
                    transcript.write_text(json.dumps(row) + "\n")
                req.update(status="compacted", finished=time.time(), clear_claim=claim)
                self.clear._write_reservation(req)
                self.assertEqual(self.clear.peer_input_state("claude", req["sid"], seat.pane, "working"),
                                 "working")

    def test_old_queue_receipt_does_not_add_a_new_compaction_wait(self):
        seat, req = self.booking("claude")
        req["idle_request"] = {"state": "accepted"}
        self.clear._write_reservation(req)
        row, text, recipient = self.pending("claude", req["sid"], seat.pane)
        with self.transport(recipient, text, state="idle"):
            self.submit(row, recipient, "idle")
        with mock.patch.object(self.clear, "validate_request",
                               side_effect=lambda *a, **k: (self.clear.read_reservation(seat.key), "")):
            claimed, why = self.clear.claim_clear(self.clear.reservation_path(seat.key), req["nonce"])
        self.assertIsNotNone(claimed)
        self.assertEqual(why, "")

    def test_compact_completion_wakes_the_existing_runner_event_input(self):
        seat, req = self.booking("claude")
        req["clear_claim"] = {"state": "sending", "command": "compact"}
        self.clear._write_reservation(req)
        self.pending("claude", req["sid"], seat.pane)
        root = peer_steward.peer_obligations.ObligationStore(self.tmp_root).root
        fd = peer_steward.shared_readiness.open_runner_wake(root)
        try:
            self.assertTrue(self.clear._finish(seat.key, req["nonce"], "compacted"))
            self.assertEqual(select.select([fd], [], [], 0)[0], [fd])
            self.assertEqual(os.read(fd, 1), b"\0")
        finally:
            os.close(fd)


class IdleCardRequestTest(_TmpRootMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        import session_tidy as st
        import session_tidy_clear as clear
        self.st, self.clear = st, clear
        import session_tidy_runner
        worker = mock.patch.object(session_tidy_runner, "_start_queued", side_effect=lambda item: item)
        worker.start()
        self.addCleanup(worker.stop)
        # This transport fixture has a known observed seat and no registered
        # work. Census completeness is exercised in session_tidy_idle.test.py.
        routes = mock.patch.object(clear, "_idle_route_reason", return_value="")
        routes.start()
        self.addCleanup(routes.stop)

    def booking(self, harness="claude", sid="sid-A", pane="w1:pX"):
        st, clear = self.st, self.clear
        seat = st.Seat("pane", st._digest("pane", pane), pane, harness, "")
        now = time.time()
        with st.seat_lock(seat.key):
            card = st.write_card(seat, harness, sid, "prior card", prompt_seq=0)
            clear._write_reservation({
                "schema": 1, "nonce": "idle-nonce", "status": "reserved", "created": now - 60,
                "deadline": now + 300, "seat": seat.as_dict(), "harness": harness,
                "sid": sid, "cwd": str(self.tmp_root), "card_generation": card["generation"],
                "prompt_seq": 0, "origin": "idle", "followup_kind": "status-only",
                "snapshot_state": "ready", "idle": {"identity": "idle-event", "due_at": now - 1,
                                                          "tokens": 200000, "min_tokens": 200000,
                                                          "compaction": clear.compaction_position(seat, harness, sid)}})
        return seat, clear.reservation_path(seat.key)

    def invoke(self, req, *, looks=None, prompt_reply=None):
        pane = req["seat"]["pane"]
        args = SimpleNamespace(request=str(self.clear.reservation_path(req["seat"]["key"])),
                               nonce=req["nonce"], target=pane)
        agent = {"pane": pane, "harness": req["harness"], "session_id": req["sid"]}
        result = {"accepted": "true"}

        def prompt(_target, text, **_kwargs):
            if prompt_reply:
                return prompt_reply(text)
            with mock.patch.object(self.st, "resolve_seat", return_value=self.seat_obj):
                self.st.run_hook(req["harness"], "prompt", req["sid"], cwd=req["cwd"], prompt_text=text)
            return 0, {}

        self.seat_obj = self.st.seat_from_dict(req["seat"])
        import session_tidy_idle
        with patch_split(peer_steward, "_herdr_missing", return_value=False), \
             mock.patch.object(session_tidy_idle, "_tokens_and_provider", return_value=(200000,"","")), \
             mock.patch.object(peer_steward, "_clear_look", side_effect=looks or [(None, agent, [])] * 3), \
             mock.patch.object(peer_steward, "_herdr_prompt", side_effect=prompt) as send, \
             mock.patch.object(peer_steward, "_record"), mock.patch("builtins.print") as output:
            rc = peer_steward.cmd_idle_card_request(args)
        return rc, output.call_args[0][0], send

    def test_busy_draft_and_question_never_type_an_idle_request_in_any_harness(self):
        for harness in ("claude", "codex", "opencode"):
            for reason in ("not-idle-working", "draft", "form-open"):
                with self.subTest(harness=harness, reason=reason):
                    seat, _path = self.booking(harness=harness, pane="idle:" + harness + reason)
                    req = self.clear.read_reservation(seat.key)
                    # A blocked look must return before the native writer or any claim.
                    rc, line, send = self.invoke(req, looks=[(reason, {}, [])])
                    send.assert_not_called()
                    self.assertNotEqual(rc, 0)
                    self.assertNotIn("idle_request", self.clear.read_reservation(seat.key))

    def test_compact_origin_request_sends_once_and_needs_the_exact_prompt_arrival_receipt(self):
        seat, path = self.booking()
        self.seat_obj = seat
        req = self.clear.read_reservation(seat.key)
        # Compact-origin main requests retain their exact native-arrival contract.
        req.update(origin="compact", compact={"identity": "compact-event",
            "card_request_required": True, "turn_end_observed": True})
        self.clear._write_reservation(req)
        with patch_split(peer_steward, "_herdr_missing", return_value=False), \
             mock.patch.object(peer_steward, "_clear_look", return_value=(None,
                 {"pane": seat.pane, "harness": "claude", "session_id": "sid-A"}, [])), \
             mock.patch.object(peer_steward, "_record"), mock.patch("builtins.print") as output, \
             mock.patch.object(peer_steward, "_herdr_prompt") as send:
            def prompt(_target, text, **_kwargs):
                with mock.patch.object(self.st, "resolve_seat", return_value=seat):
                    self.st.run_hook("claude", "prompt", "sid-A", cwd=req["cwd"], prompt_text=text)
                return 0, {}
            send.side_effect = prompt
            rc = peer_steward.cmd_idle_card_request(SimpleNamespace(request=str(path), nonce=req["nonce"],
                                                                      target=seat.pane))
        self.assertEqual(rc, 0)
        self.assertIn("accepted=true", output.call_args[0][0])
        send.assert_called_once()
        booking = self.clear.read_reservation(seat.key)
        self.assertEqual(booking["idle_request"]["state"], "sent")
        self.assertEqual(booking["idle_request"]["arrival"]["sid"], "sid-A")

    def test_idle_card_queues_once_without_main_prompt_or_arrival(self):
        import session_tidy_runner
        seat, path = self.booking()
        req = self.clear.read_reservation(seat.key)
        rc, line, send = self.invoke(req)
        self.assertEqual(rc, 0, line)
        send.assert_not_called()
        held = self.clear.read_reservation(seat.key)
        self.assertEqual(held["idle_request"]["source"], "memory-helper")
        self.assertEqual(held["idle_request"]["state"], "preparing")
        self.assertNotIn("arrival", held["idle_request"])
        item = session_tidy_runner.read_item(held["queue_id"])
        self.assertEqual((item["harness"], item["sid"]), (req["harness"], req["sid"]))
        rc, _line, send = self.invoke(req)
        self.assertNotEqual(rc, 0)
        send.assert_not_called()
        self.assertEqual(self.clear.read_reservation(seat.key)["queue_id"], item["id"])

    def test_draft_or_other_opencode_session_sends_nothing(self):
        seat, path = self.booking()
        req = self.clear.read_reservation(seat.key)
        blocked = ("draft", {"pane": seat.pane, "harness": "claude", "session_id": "sid-A"}, None)
        rc, line, sent = self.invoke(req, looks=[blocked])
        self.assertEqual((rc, line.split()[0]), (3, "accepted=queued"))
        sent.assert_not_called()
        self.assertNotIn("idle_request", self.clear.read_reservation(seat.key))

        op_seat, op_path = self.booking(harness="opencode", sid="op-sid", pane="w1:pO")
        op_req = self.clear.read_reservation(op_seat.key)
        wrong = (None, {"pane": op_seat.pane, "harness": "opencode", "session_id": "foreign"}, [])
        rc, line, sent = self.invoke(op_req, looks=[wrong])
        self.assertEqual((rc, line), (3, "accepted=skipped target=w1:pO harness=opencode session=op-sid reason=target-changed"))
        sent.assert_not_called()


class ContinueTest(_TmpRootMixin, unittest.TestCase):
    rows = ClearTest.rows

    def setUp(self):
        super().setUp()
        import session_tidy as st
        import session_tidy_clear as clear
        self.st, self.clear = st, clear

    def cleared(self, harness="claude", sid="sid-A", new="sid-B", pane="w1:pX",
                seq=0, receipt=False, window=120):
        st, clear = self.st, self.clear
        shutil.rmtree(st.state_root(), ignore_errors=True)
        seat = st.Seat("pane", st._digest("pane", pane), pane, harness, "")
        now = time.time()
        with st.seat_lock(seat.key):
            card = st.write_card(seat, harness, sid, "card body", prompt_seq=seq)
            if receipt:  # some prompt already took this card generation
                st._write_consumed(st.session_seat(seat, harness, sid),
                                   generation=card["generation"], receipts=[f"{harness}:{new}:0"])
            clear._write_reservation({
                "schema": 1, "nonce": "n0nce", "status": "cleared", "created": now - 30,
                "deadline": now + 570,
                "seat": {"kind": "pane", "key": seat.key, "pane": pane,
                         "harness": harness, "project_key": "",
                         "session": {"harness": harness, "sid": sid}},
                "harness": harness, "sid": sid, "cwd": str(self.tmp_root), "new_session": new,
                "card_generation": card["generation"], "prompt_seq": seq, "continue_off": False,
                "continued": {"state": "pending", "deadline": now + window}})
        self.seat_obj, self.path = seat, clear.reservation_path(seat.key)
        return seat

    def continue_cmd(self, nonce="n0nce"):
        printed = []
        with mock.patch.object(peer_steward.shutil, "which", return_value="/usr/bin/herdr"), \
             mock.patch.object(peer_steward, "_continue_look", return_value=None), \
             mock.patch("builtins.print", side_effect=lambda *a, **k: printed.append(" ".join(map(str, a)))):
            rc = peer_steward.main(["continue", self.seat_obj.pane, "--request", str(self.path),
                                    *(["--nonce", nonce] if nonce else [])])
        return rc, printed[-1] if printed else ""

    def booking(self):
        return json.loads(self.path.read_text(encoding="utf-8"))

    def test_claude_continue_types_once_into_the_idle_tui(self):
        self.cleared()
        guard = mock.Mock(return_value=(None, {"pid": 202}))
        with mock.patch.object(peer_steward, "_native_idle_prompt_guard", guard), \
             mock.patch.object(peer_steward, "_herdr_prompt", return_value=(0, {})) as send:
            rc, line = self.continue_cmd()
        self.assertEqual(rc, 0, line)
        self.assertIn("continued=true", line)
        self.assertIn("verify=state-flip", line)
        send.assert_called_once()
        self.assertEqual(send.call_args.args, ("w1:pX", self.clear.CONTINUE_TEXT))
        self.assertEqual(guard.call_args_list[1].kwargs, {"expected_foreground": {"pid": 202}})
        ref = line.split("transfer_ref=", 1)[1].split()[0]
        row = peer_steward.peer_message._read_pending(ref)
        # Not pending: the new session's prompt hook must not deliver it a second time.
        self.assertEqual(row["state"], "queued")
        self.assertIsNone(peer_steward.peer_message.claim_pending_herdr(
            ref, {"harness": "claude", "session_id": "sid-B", "pane_id": "w1:pX"}))

    def test_claude_continue_send_without_state_flip_is_never_retyped(self):
        self.cleared()
        with mock.patch.object(peer_steward, "_native_idle_prompt_guard",
                               return_value=(None, {"pid": 202})), \
             mock.patch.object(peer_steward, "_herdr_prompt",
                               return_value=(1, {"error": {"code": "timeout"}})):
            rc, line = self.continue_cmd()
        self.assertEqual(rc, 5, line)
        self.assertIn("continued=unverified", line)
        ref = line.split("transfer_ref=", 1)[1].split()[0]
        self.assertEqual(peer_steward.peer_message._read_pending(ref)["state"], "unverified")

    def test_continue_keeps_exact_no_trailer_ref_pending_for_native_callback(self):
        self.cleared()
        refused = mock.patch.object(peer_steward, "_native_idle_prompt_guard",
                                    return_value=("draft", None))
        no_send = mock.patch.object(peer_steward, "_herdr_prompt",
                                    side_effect=AssertionError("continue PTY send"))
        with refused, no_send:
            rc, line = self.continue_cmd()
        self.assertEqual(rc, 3, line)
        self.assertIn("continued=queued", line)
        self.assertIn("reason=draft", line)
        ref = line.split("transfer_ref=", 1)[1].split()[0]
        row = peer_steward.peer_message._read_pending(ref)
        self.assertEqual((row["state"], row["text"], row["no_trailer"], row["receipt"]),
                         ("pending", self.clear.CONTINUE_TEXT, True, "session-tidy-continue"))
        self.assertEqual(row["to"], {"harness": "claude", "session_id": "sid-B",
                                     "name": None, "pane_id": "w1:pX"})
        claim_id = line.split("claim_id=", 1)[1].split()[0]
        self.assertTrue(self.clear._finish_continue(
            self.seat_obj.key, "n0nce", "queued", "awaiting-native-receiver",
            claim_id=claim_id, transfer_ref=ref))
        booked = self.booking()["continued"]
        self.assertEqual((booked["state"], booked["transfer_ref"]), ("pending", ref))
        with refused, no_send:
            second_rc, second = self.continue_cmd()
        self.assertEqual(second_rc, 3, second)
        self.assertIn("transfer_ref=" + ref, second)
        self.assertEqual(peer_steward.peer_message._read_pending(ref)["state"], "pending")

    def test_codex_continue_uses_exact_queue_and_receipt_settles_same_booking(self):
        self.cleared(harness="codex", sid="old-thread", new="new-thread")
        observed = []
        with mock.patch.object(peer_steward.peer_message, "deliver_pending_codex",
                               side_effect=lambda ref: observed.append(ref)
                               or {"status": "queued", "reason": "native-queue-accepted"}), \
             mock.patch.object(peer_steward, "_herdr_prompt", side_effect=AssertionError("continue PTY send")), \
             mock.patch.object(peer_steward, "_read_screen", side_effect=AssertionError("composer read")):
            rc, line = self.continue_cmd()
        self.assertEqual(rc, 3, line)
        ref = line.split("transfer_ref=", 1)[1].split()[0]
        self.assertEqual(observed, [ref])
        row = peer_steward.peer_message._read_pending(ref)
        self.assertEqual((row["to"]["session_id"], row["text"], row["no_trailer"]),
                         ("new-thread", self.clear.CONTINUE_TEXT, True))
        claim_id = line.split("claim_id=", 1)[1].split()[0]
        self.assertTrue(self.clear._finish_continue(
            self.seat_obj.key, "n0nce", "queued", "native-queue-accepted",
            claim_id=claim_id, transfer_ref=ref))

        def consumed(ref_arg):
            with peer_steward.peer_message.pending_lock(ref_arg):
                current = peer_steward.peer_message._read_pending(ref_arg)
                peer_steward.peer_message._save_pending(dict(
                    current, state="received", text=None, receipt="exact-native-history"))
            return {"status": "received", "reason": "exact-native-history"}

        with mock.patch.object(peer_steward.peer_message, "deliver_pending_codex", side_effect=consumed):
            rc, line = self.continue_cmd()
        self.assertEqual(rc, 0, line)
        self.assertIn("verify=exact-native-history", line)
        self.assertEqual(self.booking()["continued"]["state"], "sending")
        claim_id = line.split("claim_id=", 1)[1].split()[0]
        self.assertTrue(self.clear._finish_continue(
            self.seat_obj.key, "n0nce", "true", "", claim_id=claim_id, transfer_ref=ref))
        self.assertEqual(self.booking()["continued"]["state"], "sent")

    def test_continue_claim_token_prevents_stale_queue_from_rewinding_newer_attempt(self):
        self.cleared()
        first, reason = self.clear.claim_continue(self.path, "n0nce")
        self.assertEqual(reason, "")
        self.assertTrue(self.clear.release_unsent_continue(first))
        second, reason = self.clear.claim_continue(self.path, "n0nce")
        self.assertEqual(reason, "")
        old_id = first["continued"]["claim_id"]
        new_id = second["continued"]["claim_id"]
        self.assertNotEqual(old_id, new_id)
        self.assertFalse(self.clear._finish_continue(
            self.seat_obj.key, "n0nce", "queued", "late", claim_id=old_id,
            transfer_ref="1" * 32))
        self.assertEqual(self.booking()["continued"]["claim_id"], new_id)
        self.assertTrue(self.clear._finish_continue(
            self.seat_obj.key, "n0nce", "queued", "native-pending", claim_id=new_id,
            transfer_ref="2" * 32))
        self.assertEqual(self.booking()["continued"]["transfer_ref"], "2" * 32)
        self.assertEqual(self.booking()["continued"]["state"], "pending")

    def test_continue_needs_no_start_receipt_and_stops_only_for_a_taken_card(self):
        # Claude's SessionStart renders no card (2026-11-10: a tidy clear waited for a start
        # receipt that never comes and the person had to type).
        for harness in ("claude", "codex", "opencode"):
            for receipt, expected in ((False, None), (True, "card-taken")):
                with self.subTest(harness=harness, receipt=receipt):
                    self.cleared(harness=harness, receipt=receipt)
                    self.assertEqual(peer_steward._continue_card_reason(self.booking()), expected)

    def test_continue_requires_exact_new_sid_and_idle_without_screen_reads(self):
        self.cleared()
        req = self.booking()
        info = {"pane_id": self.seat_obj.pane, "shell_pid": 101,
                "foreground_process_group_id": 202,
                "foreground_processes": [{"pid": 202, "argv": ["claude"]}]}
        for sid, native_sid, state, expected in (("sid-B", "sid-B", "idle", None),
                                                 ("sid-B", "old-sid", "idle", "target-changed"),
                                                 ("sid-B", "sid-B", "blocked", "not-idle-blocked")):
            ident = {"harness": "claude", "session_id": sid, "pane": self.seat_obj.pane, "name": "child"}
            with self.subTest(sid=sid, state=state), \
                 mock.patch.object(peer_steward, "_retire_target",
                                   return_value=(state, ident, 0, None)), \
                 mock.patch.object(peer_steward, "_retire_pane_info", return_value=info), \
                 mock.patch.object(peer_steward, "_continue_native_session", return_value=native_sid), \
                 mock.patch.object(peer_steward, "_pane_readiness",
                                   return_value=type("Ready", (), {"state": "ready", "reason": ""})()), \
                 mock.patch.object(peer_steward, "_read_screen", side_effect=AssertionError("composer read")):
                self.assertEqual(peer_steward._continue_look(self.seat_obj.pane, req), expected)

    def test_continue_is_not_held_back_by_the_sessions_own_dispatched_work(self):
        self.cleared()
        req = self.booking()
        info = {"pane_id": self.seat_obj.pane, "shell_pid": 101,
                "foreground_process_group_id": 202,
                "foreground_processes": [{"pid": 202, "argv": ["claude"]}]}
        ident = {"harness": "claude", "session_id": "sid-B", "pane": self.seat_obj.pane, "name": "sup"}

        def readiness(*_a, purpose="completion", **_k):
            # A supervisor whose owners still run: pending for completion, ready for input.
            state, reason = (("ready", "native-turn-ready") if purpose == "input"
                             else ("pending", "bound-registered-work-pending"))
            return type("Readiness", (), {"state": state, "reason": reason})()

        with mock.patch.object(peer_steward, "_retire_target",
                               return_value=("idle", ident, 0, None)), \
             mock.patch.object(peer_steward, "_retire_pane_info", return_value=info), \
             mock.patch.object(peer_steward, "_continue_native_session", return_value="sid-B"), \
             mock.patch.object(peer_steward, "_pane_readiness", side_effect=readiness):
            self.assertIsNone(peer_steward._continue_look(self.seat_obj.pane, req))

    def test_user_prompt_card_handoff_and_expiry_still_stop_continue(self):
        def bump(seat):
            with self.st.seat_lock(seat.key):
                self.st.bump_prompt_seq(seat, "claude", "sid-B", time.time())

        def rewrite(seat):
            with self.st.seat_lock(seat.key):
                self.st.write_card(seat, "claude", self.booking()["sid"], "new card")

        def expired(_seat):
            data = self.booking()
            data["continued"]["deadline"] = time.time() - 1
            self.path.write_text(json.dumps(data), encoding="utf-8")

        for change, reason in ((bump, "new-input"), (rewrite, "card-changed"),
                               (lambda seat: self.st.mark_card_handed_off(
                                   self.st.session_seat(seat, "claude", self.booking()["sid"])),
                                "handed-off"), (expired, "expired")):
            with self.subTest(reason=reason):
                change(self.cleared())
                with mock.patch.object(peer_steward, "_herdr_prompt",
                                       side_effect=AssertionError("continued an invalid booking")):
                    rc, line = self.continue_cmd()
                self.assertEqual(rc, 3, line)
                self.assertIn("reason=" + reason, line)
                self.assertFalse(self.booking().get("continued", {}).get("transfer_ref"))

    def test_herdr_missing_remains_a_typed_failure(self):
        self.cleared()
        printed = []
        with mock.patch.object(peer_steward.shutil, "which", return_value=None), \
             mock.patch("builtins.print", side_effect=lambda *a, **k: printed.append(" ".join(map(str, a)))):
            rc = peer_steward.main(["continue", self.seat_obj.pane, "--request", str(self.path)])
        self.assertEqual((rc, printed[-1]), (1, "continued=failed reason=herdr-not-found"))

if __name__ == "__main__":
    unittest.main()
