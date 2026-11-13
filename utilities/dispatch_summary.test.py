#!/usr/bin/env python3
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import json
import fcntl
import os
from pathlib import Path
import shlex
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
import warnings
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "utilities"), str(ROOT / "tools")]

import dispatch_summary as S  # noqa: E402
import dispatch_contract as D  # noqa: E402
from fleet import titles  # noqa: E402
from split_module_fixture import patch_split  # noqa: E402

warnings.filterwarnings("ignore", category=ResourceWarning)


class DispatchSummaryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_env = os.environ.copy()
        os.environ.update({
            "FLEET_TITLE_STATE_DIR": str(Path(self.tmp.name) / "titles"),
            "AGENT_MODEL_GOVERNOR_ROOT": str(Path(self.tmp.name) / "governor"),
            # `refresh_title.run_worker` resolves the governor script through
            # `AGENT_HOME`, which in a real session points at the installed
            # release, not this checkout -- pin it here (it also reaches the
            # spawned `refresh_title.py` subprocess through inherited env) so
            # the test exercises this worktree's governor.
            "AGENT_HOME": str(ROOT),
            "AGENT_MODEL_WORKER_TOTAL": "5",
            "AGENT_MODEL_WORKER_START_BUDGET": "20",
            "FLEET_TITLE_CONCURRENCY": "4",
            "FLEET_TITLE_MAX_STARTS": "4",
            "FLEET_TITLE_COMMAND": shlex.join([
                sys.executable, "-c",
                "print('TITLE: 분사 작업 요약\\nNOW: 분사 작업 요약을 갱신하고 있습니다')",
            ]),
        })
        os.environ.pop("FLEET_TITLE_DISABLE", None)
        os.environ.pop("FLEET_TITLE_REFRESH", None)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.old_env)
        self.tmp.cleanup()

    def test_home_consumer_uses_recorded_state_root_and_provider_lock(self):
        attempt = "att-home-consumer"
        harness = "claude"
        recorded_root = Path(self.tmp.name) / "recorded-title-state"
        state_path = recorded_root / ".dispatch-owners" / harness / f"{attempt}.json"
        state_path.parent.mkdir(parents=True)
        runtime_home = Path(self.tmp.name) / "worker-home"
        runtime_home.mkdir()
        transcript = runtime_home / "projects" / "session.jsonl"
        transcript.parent.mkdir()
        transcript.write_text("{}\n", encoding="utf-8")
        owner_start = S.process_observation(os.getpid())[1]
        namespace = S.process_namespace_identity()
        sid = S.summary_sid(attempt)
        state_path.write_text(json.dumps({
            "schema_version": S.OWNER_SCHEMA,
            "status": "active",
            "attempt_id": attempt,
            "harness": harness,
            "sid": sid,
            "transcript": str(transcript),
            "runtime_home": str(runtime_home),
            "pid": os.getpid(),
            "proc_start": owner_start,
            "observer_namespace": namespace,
        }), encoding="utf-8")
        owner_lock = state_path.with_suffix(".lock")
        owner_lock.touch()
        metadata = {
            "attempt_id": attempt,
            "harness": harness,
            "runtime_home": str(runtime_home),
            "log_file": str(transcript),
            "summary_owner": S.OWNER_KIND,
            "summary_sid": sid,
            "summary_owner_pid": str(os.getpid()),
            "summary_owner_pid_start": owner_start,
            "summary_owner_pid_observer_ns": namespace,
            "summary_state_file": str(state_path),
        }
        fd = os.open(owner_lock, os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            with mock.patch.dict(os.environ, {"FLEET_TITLE_STATE_DIR": str(Path(self.tmp.name) / "wrong-root")}):
                self.assertEqual(S.home_consumer_evidence(metadata), ("live", "owner-lock-held"))
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

        provider_parent = recorded_root / harness
        provider_parent.mkdir()
        provider_lock = provider_parent / (".lock-" + sid)
        provider_lock.mkdir()
        old = time.time() - 30 * 86400
        os.utime(provider_lock, (old, old))
        with mock.patch.dict(os.environ, {"FLEET_TITLE_STATE_DIR": str(Path(self.tmp.name) / "wrong-root")}):
            # Intentional change: an old lockdir that no process names holds nothing.
            self.assertEqual(S.home_consumer_evidence(metadata),
                             ("absent", "provider-lockdir-unreferenced"))
            link = Path(self.tmp.name) / "lockdir-link"
            link.symlink_to(provider_lock)
            for spelled in (["--lockdir", str(provider_lock)],
                            ["--lockdir=" + str(provider_lock)],
                            ["--lockdir", str(link)]):
                with self.subTest(argv=spelled[0]):
                    provider = subprocess.Popen(
                        [sys.executable, "-c", "import time; time.sleep(30)", *spelled])
                    try:
                        self.assertEqual(S.home_consumer_evidence(metadata),
                                         ("live", "provider-lockdir-present"))
                    finally:
                        provider.kill()
                        provider.wait(timeout=5)
            with patch_split(S, "process_namespace_identity", return_value="pid:[other]"), \
                    patch_split(S, "_current_observer_is_host_like", return_value=False):
                self.assertEqual(S.home_consumer_evidence(metadata),
                                 ("unknown", "provider-scan-namespace-unverifiable"))
            # A host-like reader walks every PID namespace: the extinct recorded
            # namespace is no obstacle, and absence is decided by the argv scan.
            with patch_split(S, "_current_observer_is_host_like", return_value=True):
                stale = {**metadata, "summary_owner_pid_observer_ns": "pid:[4026534399]"}
                original_state = state_path.read_text()
                state_path.write_text(json.dumps({
                    **json.loads(original_state), "observer_namespace": "pid:[4026534399]"}))
                self.assertEqual(S.home_consumer_evidence(stale),
                                 ("absent", "provider-lockdir-unreferenced"))
                provider = subprocess.Popen(
                    [sys.executable, "-c", "import time; time.sleep(30)",
                     "--lockdir", str(provider_lock)])
                try:
                    self.assertEqual(S.home_consumer_evidence(stale),
                                     ("live", "provider-lockdir-present"))
                finally:
                    provider.kill()
                    provider.wait(timeout=5)
            with patch_split(S, "_current_observer_is_host_like", return_value=False):
                self.assertEqual(S.home_consumer_evidence(stale),
                                 ("unknown", "provider-scan-namespace-unverifiable"))
            state_path.write_text(original_state)
        provider_lock.rmdir()
        self.assertEqual(S.home_consumer_evidence(metadata)[0], "absent")

    def test_owner_state_without_runtime_home_is_not_a_conflict(self):
        attempt = "att-legacy-owner-state"
        harness = "claude"
        root = Path(self.tmp.name) / "recorded-title-state"
        state_path = root / ".dispatch-owners" / harness / f"{attempt}.json"
        state_path.parent.mkdir(parents=True)
        home = Path(self.tmp.name) / "worker-home"
        home.mkdir()
        sid = S.summary_sid(attempt)
        start = S.process_observation(os.getpid())[1]
        namespace = S.process_namespace_identity()
        state = {"schema_version": S.OWNER_SCHEMA, "status": "active", "attempt_id": attempt,
                 "harness": harness, "sid": sid, "transcript": str(home / "t.jsonl"),
                 "pid": os.getpid(), "proc_start": start, "observer_namespace": namespace}
        state_path.write_text(json.dumps(state))
        state_path.with_suffix(".lock").touch()
        metadata = {
            "attempt_id": attempt, "harness": harness, "runtime_home": str(home),
            "summary_owner": S.OWNER_KIND, "summary_sid": sid,
            "summary_owner_pid": str(os.getpid()), "summary_owner_pid_start": start,
            "summary_owner_pid_observer_ns": namespace, "summary_state_file": str(state_path),
        }
        self.assertEqual(S.home_consumer_evidence(metadata)[0], "absent")
        fd = os.open(state_path.with_suffix(".lock"), os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            self.assertEqual(S.home_consumer_evidence(metadata), ("live", "owner-lock-held"))
        finally:
            os.close(fd)
        state_path.write_text(json.dumps({**state, "runtime_home": str(home / "other")}))
        self.assertEqual(S.home_consumer_evidence(metadata), ("unknown", "state-home-conflict"))

    def test_summary_owner_admits_path_based_reads_of_identityless_legacy_home(self):
        import threading
        import worker_home_retention as retention
        from worker_runtime_home import _builder

        attempt = "att-summary-legacy-admission"
        root = Path(self.tmp.name) / "dispatch-state"
        jobs = root / "jobs.log"
        root.mkdir()
        jobs.write_text("")
        home_key = _builder.worker_home_key(str(ROOT), "claude", "stage", attempt)
        home = root / "homes" / "workers" / home_key
        (home / "projects").mkdir(parents=True)
        (home / "worker-home.json").write_text(json.dumps({
            "harness": "claude", "worker_type": "stage", "source": str(ROOT),
            "profile": None,
        }))
        transcript = home / "projects" / "transcript.jsonl"
        transcript.write_text("native legacy transcript\n")
        owner_log = Path(self.tmp.name) / f"owner.{attempt}.claude.jsonl"
        owner_log.write_text("")
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(0.8)"],
                                 start_new_session=True)
        target_start = S.process_observation(child.pid)[1]
        observed, finished = threading.Event(), threading.Event()
        result, errors = [], []
        original_source = S._summary_source

        def source(*args, **kwargs):
            observed.set()
            return original_source(*args, **kwargs)

        def run_owner():
            try:
                result.append(S.supervise(
                    attempt_id=attempt, harness="claude", transcript=owner_log,
                    target_pid=child.pid, target_start=target_start, poll=0.02,
                    initial_delay=60, final_grace=0, log_quiet=0,
                    runtime_home=home, attempt_cwd="/wt/owner"))
            except BaseException as exc:  # reported on the test thread
                errors.append(repr(exc))
            finally:
                finished.set()

        thread = threading.Thread(target=run_owner)
        try:
            with mock.patch.dict(os.environ, {"AGENT_DISPATCH_JOBS": str(jobs)}), \
                    mock.patch.object(S, "_summary_source", source):
                thread.start()
                self.assertTrue(observed.wait(3), "summary owner did not reach native-source resolution")
                # The owner pins exactly this HOME (shared flock on the leaf the
                # retention detach must lock exclusively), not a global lock.
                fd = os.open(home, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                finally:
                    os.close(fd)
                self.assertTrue(finished.wait(3))
        finally:
            thread.join(timeout=3)
            if child.poll() is None:
                child.terminate()
            child.wait(timeout=3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(result, [0])

    def test_first_concrete_claude_model_is_recorded_once(self):
        attempt = "att-resolved-model"
        log = Path(self.tmp.name) / f"owner.{attempt}.claude.jsonl"
        jobs = Path(self.tmp.name) / "jobs.log"
        jobs.write_text(
            "2026-08-04T00:00:00Z\topen\t/repo\t/wt\towner\t"
            "attempt_schema_version=2,dispatch_depth=1,transport=headless,"
            "execution_surface=registered-headless,registered_worker=1,"
            "fallback_hop=same-harness-headless,worker_type=owner,"
            f"attempt_id={attempt},harness=claude,model=opus,log_file={log}\n",
            encoding="utf-8",
        )
        log.write_text(
            json.dumps({"type": "system", "subtype": "init", "model": "opus"}) + "\n"
            + json.dumps({"type": "assistant", "message": {"model": "claude-opus-5-5"}}) + "\n",
            encoding="utf-8",
        )
        cursor = {}
        self.assertTrue(S._record_resolved_model(log, jobs, attempt, cursor))
        self.assertEqual(D.parse_registry_metadata(jobs.read_text().split("\t")[5])["model"], "opus")
        self.assertEqual(D.parse_registry_metadata(jobs.read_text().strip().split("\t")[5])["resolved_model"],
                         "claude-opus-5-5")
        with log.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"type": "assistant", "message": {"model": "claude-opus-6"}}) + "\n")
        self.assertFalse(S._record_resolved_model(log, jobs, attempt, cursor))
        self.assertEqual(D.parse_registry_metadata(jobs.read_text().strip().split("\t")[5])["resolved_model"],
                         "claude-opus-5-5")

    def test_prompt_validator_accepts_exact_sibling_and_rejects_neighbor(self):
        attempt = "att-prompt-validator"
        directory = Path(self.tmp.name)
        log = directory / f"owner.{attempt}.codex.jsonl"
        prompt = directory / f"owner.{attempt}.codex.prompt.txt"
        neighbor = directory / "owner.other.codex.prompt.txt"
        log.write_text("{}\n", encoding="utf-8")
        prompt.write_text("Assignment:\nanchor\n", encoding="utf-8")
        neighbor.write_text("neighbor\n", encoding="utf-8")
        self.assertEqual(S._validate_prompt(prompt, S._validate_transcript(log, "codex", attempt)), prompt)
        with self.assertRaises(ValueError):
            S._validate_prompt(neighbor, S._validate_transcript(log, "codex", attempt))

    def test_supervisor_generates_initial_and_final_sidecar_without_fleet(self):
        attempt = "att-summary-live"
        log = Path(self.tmp.name) / f"owner.{attempt}.codex.jsonl"
        log.write_text(json.dumps({
            "type": "item.completed",
            "item": {"type": "agent_message", "text": "start summary owner"},
        }) + "\n", encoding="utf-8")
        child = subprocess.Popen([
            sys.executable, "-c",
            "import pathlib,sys,time,json; time.sleep(.4); "
            "p=pathlib.Path(sys.argv[1]); "
            "f=p.open('a'); f.write(json.dumps({'type':'item.completed','item':"
            "{'type':'agent_message','text':'finish summary owner'}})+'\\n'); "
            "f.close(); time.sleep(.25)",
            str(log),
        ], start_new_session=True)
        start = S.process_observation(child.pid)[1]
        self.assertTrue(start)
        rc = S.supervise(
            attempt_id=attempt, harness="codex", transcript=log,
            target_pid=child.pid, target_start=start,
            poll=0.05, initial_delay=0, periodic_debounce=90,
            final_grace=8, log_quiet=0.05,
        )
        child.wait(timeout=5)
        self.assertEqual(rc, 0)
        sidecar = titles.read(S.summary_sid(attempt), harness="codex")
        self.assertEqual(sidecar["title"], "분사 작업 요약")
        self.assertEqual(sidecar["summary"], "분사 작업 요약을 갱신하고 있습니다")
        self.assertEqual(sidecar["offset"], log.stat().st_size)
        state = json.loads(S.owner_state_path("codex", attempt).read_text())
        self.assertEqual(state["status"], "terminal")
        self.assertTrue(state["final_refresh_complete"])

    def test_periodic_refresh_uses_ten_minute_debounce_without_priority(self):
        """A live dispatch child's periodic title refresh must not spend the
        rolling start budget the way a 90s cadence did (intent decision
        `child-title-interval`); initial/final stay immediate and priority."""
        attempt = "att-periodic-cadence"
        directory = Path(self.tmp.name)
        log = directory / f"owner.{attempt}.codex.jsonl"
        log.write_text(json.dumps({
            "type": "item.completed",
            "item": {"type": "agent_message", "text": "start"},
        }) + "\n", encoding="utf-8")
        script = directory / "grow_transcript.py"
        script.write_text(
            "import json, pathlib, sys, time\n"
            "p = pathlib.Path(sys.argv[1])\n"
            "for i in range(6):\n"
            "    with p.open('a', encoding='utf-8') as f:\n"
            "        f.write(json.dumps({'type': 'item.completed', 'item': "
            "{'type': 'agent_message', 'text': 'chunk-%d' % i}}) + chr(10))\n"
            "    time.sleep(0.05)\n"
            "time.sleep(0.1)\n",
            encoding="utf-8",
        )
        child = subprocess.Popen(
            [sys.executable, str(script), str(log)], start_new_session=True
        )
        start = S.process_observation(child.pid)[1]
        self.assertTrue(start)
        calls = []

        def fake_refresh(*args, **kwargs):
            calls.append(kwargs)
            return True

        with mock.patch.object(S, "_refresh", side_effect=fake_refresh):
            rc = S.supervise(
                attempt_id=attempt, harness="codex", transcript=log,
                target_pid=child.pid, target_start=start,
                poll=0.02, initial_delay=0,
                final_grace=1, log_quiet=0.05,
            )
        child.wait(timeout=5)
        self.assertEqual(rc, 0)
        phases = [call["phase"] for call in calls]
        self.assertEqual(phases[0], "initial")
        self.assertEqual(phases[-1], "final")
        periodic_calls = [call for call in calls if call["phase"] == "periodic"]
        self.assertTrue(periodic_calls, "expected at least one periodic refresh attempt")
        for call in periodic_calls:
            self.assertEqual(call["debounce"], S.DEFAULT_PERIODIC_DEBOUNCE)
            self.assertIs(call["priority"], False)
        self.assertEqual(calls[0]["debounce"], 0)
        self.assertIs(calls[0]["priority"], True)
        self.assertEqual(calls[-1]["debounce"], 0)
        self.assertIs(calls[-1]["priority"], True)

    def _supervise_with_fake_refresh(self, attempt, results, alive_seconds=0.6):
        """Run supervise against a short-lived child; `results(phase)` is the value
        the fake `_refresh` returns. Returns the phases it was called with."""
        log = Path(self.tmp.name) / f"owner.{attempt}.codex.jsonl"
        log.write_text(json.dumps({
            "type": "item.completed",
            "item": {"type": "agent_message", "text": "start"},
        }) + "\n", encoding="utf-8")
        child = subprocess.Popen(
            [sys.executable, "-c", f"import time; time.sleep({alive_seconds})"],
            start_new_session=True,
        )
        start = S.process_observation(child.pid)[1]
        self.assertTrue(start)
        phases = []

        def fake_refresh(*args, **kwargs):
            phases.append(kwargs["phase"])
            return results(kwargs["phase"])

        with mock.patch.object(S, "_refresh", side_effect=fake_refresh):
            rc = S.supervise(
                attempt_id=attempt, harness="codex", transcript=log,
                target_pid=child.pid, target_start=start,
                poll=0.02, initial_delay=0, final_grace=1, log_quiet=0.05,
            )
        child.wait(timeout=5)
        self.assertEqual(rc, 0)
        return phases

    def test_failed_initial_is_retried_on_every_poll(self):
        phases = self._supervise_with_fake_refresh(
            "att-initial-retry", lambda phase: False)
        initial = [phase for phase in phases if phase == "initial"]
        self.assertGreater(len(initial), 3)
        self.assertNotIn("periodic", phases)

    def test_periodic_attempts_are_spaced_by_the_attempt_interval(self):
        phases = self._supervise_with_fake_refresh(
            "att-periodic-spacing", lambda phase: True)
        self.assertEqual(phases[0], "initial")
        # Many 20ms polls fit in the child's lifetime, yet only one periodic
        # attempt fits in one 15s interval.
        self.assertEqual(phases.count("periodic"), 1)
        with mock.patch.object(S, "PERIODIC_ATTEMPT_INTERVAL", 0.1):
            phases = self._supervise_with_fake_refresh(
                "att-periodic-short", lambda phase: True, alive_seconds=1.0)
        self.assertGreater(phases.count("periodic"), 1)

    def test_final_refresh_is_not_delayed_by_the_periodic_interval(self):
        started = time.monotonic()
        phases = self._supervise_with_fake_refresh(
            "att-final-prompt", lambda phase: True)
        self.assertEqual(phases[-1], "final")
        self.assertLess(time.monotonic() - started, S.PERIODIC_ATTEMPT_INTERVAL)

    def test_reconcile_reattaches_only_one_live_exact_attempt(self):
        attempt = "att-summary-recover"
        worker = subprocess.Popen(["sleep", "60"], start_new_session=True)
        owner = subprocess.Popen(["sleep", "60"], start_new_session=True)
        try:
            start = S.process_observation(worker.pid)[1]
            log = Path(self.tmp.name) / f"owner.{attempt}.claude.jsonl"
            log.write_text('{"message":"recover summary"}\n', encoding="utf-8")
            prompt = Path(self.tmp.name) / f"owner.{attempt}.claude.prompt.txt"
            prompt.write_text("Assignment:\nRetained exact anchor\n", encoding="utf-8")
            jobs = Path(self.tmp.name) / "jobs.log"
            jobs.write_text(
                "2026-08-04T00:00:00Z\topen\t/repo\t/wt\towner\t"
                "attempt_schema_version=2,dispatch_depth=1,transport=headless,"
                "execution_surface=registered-headless,registered_worker=1,"
                "fallback_hop=same-harness-headless,worker_type=owner,"
                f"attempt_id={attempt},harness=claude,pid={worker.pid},"
                f"pid_start={start},runtime_home={self.tmp.name}/worker-home,log_file={log}\n",
                encoding="utf-8",
            )
            owner_start = S.process_observation(owner.pid)[1]
            observer_namespace = S.process_namespace_identity()
            attached = {
                "summary_owner": S.OWNER_KIND,
                "summary_sid": S.summary_sid(attempt),
                "summary_owner_pid": str(owner.pid),
                "summary_owner_pid_start": owner_start,
                "summary_owner_pid_observer_ns": observer_namespace,
                "summary_state_file": str(S.owner_state_path("claude", attempt)),
            }
            state_path = S.owner_state_path("claude", attempt)
            state_path.parent.mkdir(parents=True, exist_ok=True)
            state_path.write_text(json.dumps({
                "schema_version": S.OWNER_SCHEMA,
                "status": "active",
                "attempt_id": attempt,
                "harness": "claude",
                "pid": owner.pid,
                "proc_start": owner_start,
                "observer_namespace": observer_namespace,
                "prompt_path": str(prompt),
            }))
            with mock.patch.object(S, "launch_summary_owner", return_value=attached) as launch:
                first = S.ensure_attempt_owner(jobs, attempt)
                second = S.ensure_attempt_owner(jobs, attempt)
            self.assertEqual(first["state"], "started")
            self.assertEqual(second, {"state": "existing", "reason": "owner-live"})
            launch.assert_called_once()
            self.assertEqual(launch.call_args.kwargs["prompt_path"], prompt)
            self.assertEqual(launch.call_args.kwargs["runtime_home"],
                             f"{self.tmp.name}/worker-home")
            self.assertEqual(launch.call_args.kwargs["attempt_cwd"], "/wt")
            self.assertIn("summary_owner=dispatch-v1", jobs.read_text())
        finally:
            for process in (worker, owner):
                if process.poll() is None:
                    process.kill()
                process.wait()

    def test_reconcile_invalid_retained_prompt_degrades_without_globbing(self):
        worker = subprocess.Popen(["sleep", "60"], start_new_session=True)
        try:
            start = S.process_observation(worker.pid)[1]
            observer_namespace = S.process_namespace_identity()
            invalid_paths = {
                "missing": Path(self.tmp.name) / "owner.att-missing.claude.prompt.txt",
                "moved": Path(self.tmp.name) / "moved.prompt.txt",
                "symlink": Path(self.tmp.name) / "owner.att-symlink.claude.prompt.txt",
                "foreign": Path(self.tmp.name) / "owner.att-foreign.codex.prompt.txt",
                "suffix": Path(self.tmp.name) / "owner.att-suffix.claude.txt",
            }
            invalid_paths["moved"].write_text("moved\n", encoding="utf-8")
            foreign_target = invalid_paths["foreign"]
            foreign_target.write_text("foreign\n", encoding="utf-8")
            invalid_paths["symlink"].symlink_to(foreign_target)
            for label, retained in invalid_paths.items():
                attempt = "att-invalid-" + label
                log = Path(self.tmp.name) / f"owner.{attempt}.claude.jsonl"
                log.write_text('{"message":"recover summary"}\n', encoding="utf-8")
                jobs = Path(self.tmp.name) / (label + ".jobs.log")
                jobs.write_text(
                    "2026-08-04T00:00:00Z\topen\t/repo\t/wt\towner\t"
                    "attempt_schema_version=2,dispatch_depth=1,transport=headless,"
                    "execution_surface=registered-headless,registered_worker=1,"
                    "fallback_hop=same-harness-headless,worker_type=owner,"
                    f"attempt_id={attempt},harness=claude,pid={worker.pid},"
                    f"pid_start={start},log_file={log}\n",
                    encoding="utf-8",
                )
                state_path = S.owner_state_path("claude", attempt)
                state_path.parent.mkdir(parents=True, exist_ok=True)
                state_path.write_text(json.dumps({
                    "schema_version": S.OWNER_SCHEMA,
                    "status": "terminal",
                    "attempt_id": attempt,
                    "harness": "claude",
                    "pid": 0,
                    "proc_start": "not-live",
                    "observer_namespace": observer_namespace,
                    "prompt_path": str(retained),
                }))
                attached = {"summary_owner": S.OWNER_KIND, "summary_sid": S.summary_sid(attempt)}
                with mock.patch.object(S, "launch_summary_owner", return_value=attached) as launch:
                    result = S.ensure_attempt_owner(jobs, attempt)
                self.assertEqual(result["state"], "started", label)
                self.assertIsNone(launch.call_args.kwargs["prompt_path"], label)
        finally:
            if worker.poll() is None:
                worker.kill()
            worker.wait()

    def test_launch_summary_owner_passes_one_prompt_pair_without_contents(self):
        attempt = "att-summary-argv"
        worker = subprocess.Popen(["sleep", "60"], start_new_session=True)
        try:
            start = S.process_observation(worker.pid)[1]
            log = Path(self.tmp.name) / f"owner.{attempt}.codex.jsonl"
            prompt = Path(self.tmp.name) / f"owner.{attempt}.codex.prompt.txt"
            log.write_text("{}\n", encoding="utf-8")
            prompt.write_text("Assignment:\nsecret anchor contents\n", encoding="utf-8")
            captured = {}

            class FakeOwner:
                pid = 54321

                def poll(self):
                    return None

            def fake_popen(argv, **kwargs):
                captured["argv"] = list(argv)
                captured["env"] = dict(kwargs["env"])
                return FakeOwner()

            def fake_identity(_pid):
                state_path = S.owner_state_path("codex", attempt)
                state_path.write_text(json.dumps({
                    "schema_version": S.OWNER_SCHEMA,
                    "status": "active",
                    "attempt_id": attempt,
                    "harness": "codex",
                    "pid": 54321,
                    "proc_start": "owner-start",
                    "observer_namespace": S.process_namespace_identity(),
                }))
                return {"pid_start": "owner-start"}

            with mock.patch.object(S.subprocess, "Popen", side_effect=fake_popen), \
                    patch_split(S, "process_launch_identity", side_effect=fake_identity):
                S.launch_summary_owner(
                    attempt_id=attempt, harness="codex", transcript=log,
                    target_pid=worker.pid, target_start=start, prompt_path=prompt,
                    runtime_home=Path(self.tmp.name) / "worker-home",
                    attempt_cwd="/exact/worktree",
                )
            self.assertEqual(captured["argv"].count("--prompt"), 1)
            prompt_index = captured["argv"].index("--prompt")
            self.assertEqual(captured["argv"][prompt_index + 1], str(prompt))
            self.assertEqual(captured["argv"][captured["argv"].index("--runtime-home") + 1],
                             str(Path(self.tmp.name) / "worker-home"))
            self.assertEqual(captured["argv"][captured["argv"].index("--attempt-cwd") + 1],
                             "/exact/worktree")
            self.assertNotIn("secret anchor contents", " ".join(captured["argv"]))
            self.assertNotIn("secret anchor contents", " ".join(captured["env"].values()))
        finally:
            if worker.poll() is None:
                worker.kill()
            worker.wait()

    def _announce(self, session_id: str, cwd: str) -> str:
        return json.dumps({
            "type": "dispatch.supervisor.session",
            "parent_attempt_id": "att-x",
            "session_id": session_id,
            "cwd": cwd,
        }) + "\n"

    def test_supervised_receipt_log_is_never_its_own_summary_source(self):
        """A supervised owner's receipt log carries no model text.

        Regression: refreshing against it wrote an empty title with no NOW line and
        advanced the cursor past the whole run, so the sub-session stayed nameless
        for its entire lifetime.
        """
        log = Path(self.tmp.name) / "owner.att-sup.claude.jsonl"
        log.write_text(
            self._announce("11111111-2222-3333-4444-555555555555", "/wt")
            + json.dumps({"type": "result", "subtype": "success", "result": "PASS"}) + "\n",
            encoding="utf-8",
        )
        decided, announced = S.announced_session(log)
        self.assertTrue(decided)
        self.assertEqual(announced["session_id"], "11111111-2222-3333-4444-555555555555")
        # No transcript on disk yet -> wait, never fall back to the receipt log.
        self.assertIsNone(S._summary_source(log, {}))

    def test_terminal_supervised_source_absence_degrades_without_refreshing_receipt(self):
        attempt = "att-no-native-source"
        session_id = "11111111-2222-3333-4444-555555555555"
        log = Path(self.tmp.name) / f"owner.{attempt}.claude.jsonl"
        log.write_text(self._announce(session_id, "/missing-worktree"), encoding="utf-8")
        child = subprocess.Popen(["sleep", "0.1"], start_new_session=True)
        start = S.process_observation(child.pid)[1]
        with mock.patch.object(S, "_refresh", side_effect=AssertionError("receipt refresh")):
            self.assertEqual(S.supervise(
                attempt_id=attempt, harness="claude", transcript=log,
                target_pid=child.pid, target_start=start, poll=0.02,
                initial_delay=0, final_grace=0.1, log_quiet=0.01,
                runtime_home=Path(self.tmp.name) / "worker-home",
                attempt_cwd="/missing-worktree"), 0)
        child.wait(timeout=2)
        state = json.loads(S.owner_state_path("claude", attempt).read_text())
        self.assertEqual(state["status"], "degraded")
        self.assertEqual(state["summary_source_reason"], "summary_source_unavailable")
        self.assertFalse(state["final_refresh_started"])
        self.assertIsNone(titles.read(S.summary_sid(attempt), harness="claude"))

    def test_supervisor_never_recreates_a_removed_owner_state_directory(self):
        # A fixture's temporary state home can be removed while its detached
        # supervisor still follows the attempt. The supervisor's later state
        # writes used to recreate the owner tree mid-removal (Errno 39 on CI).
        import shutil
        attempt = "att-owner-root-removed"
        log = Path(self.tmp.name) / f"owner.{attempt}.codex.jsonl"
        log.write_text("", encoding="utf-8")
        child = subprocess.Popen(["sleep", "0.2"], start_new_session=True)
        start = S.process_observation(child.pid)[1]
        owner_dir = S.owner_state_path("codex", attempt).parent
        real_proc_live = S._proc_live
        removed = []

        def remove_owner_root_once(pid, pid_start):
            if not removed:
                self.assertTrue(S.owner_state_path("codex", attempt).is_file())
                shutil.rmtree(S.owner_root())
                removed.append(True)
            return real_proc_live(pid, pid_start)

        with mock.patch.object(S, "_proc_live", side_effect=remove_owner_root_once), \
                mock.patch.object(S, "_refresh", return_value=False):
            self.assertEqual(S.supervise(
                attempt_id=attempt, harness="codex", transcript=log,
                target_pid=child.pid, target_start=start, poll=0.02,
                initial_delay=0, final_grace=0.1, log_quiet=0.01), 0)
        child.wait(timeout=2)
        self.assertEqual(removed, [True])
        self.assertFalse(owner_dir.exists())
        self.assertFalse(S.owner_root().exists())

    def test_duplicate_supervisor_announcements_are_invalid(self):
        log = Path(self.tmp.name) / "owner.att-duplicate.claude.jsonl"
        event = self._announce("11111111-2222-3333-4444-555555555555", "/wt")
        log.write_text(event + event, encoding="utf-8")
        decided, announced = S.announced_session(log)
        self.assertTrue(decided)
        self.assertEqual(announced["invalid"], "multiple-supervisor-session-events")
        self.assertIsNone(S._summary_source(log, {}))

    def test_late_supervisor_announcement_after_control_and_receipt_rows_is_used(self):
        log = Path(self.tmp.name) / "owner.att-late.claude.jsonl"
        session_id = "11111111-2222-3333-4444-555555555555"
        log.write_text(
            json.dumps({"type": "dispatch.supervisor.started"}) + "\n"
            + json.dumps({"type": "result", "result": "PASS"}) + "\n"
            + self._announce(session_id, "/wt"), encoding="utf-8")
        decided, announced = S.announced_session(log)
        self.assertTrue(decided)
        self.assertEqual(announced["session_id"], session_id)
        waiting = Path(self.tmp.name) / "owner.att-late-wait.claude.jsonl"
        waiting.write_text(json.dumps({"type": "dispatch.supervisor.started"}) + "\n")
        self.assertEqual(S.announced_session(waiting), (False, None))
        waiting.write_text(waiting.read_text() + self._announce(session_id, "/wt"),
                           encoding="utf-8")
        self.assertEqual(S.announced_session(waiting)[1]["session_id"], session_id)
        missing = Path(self.tmp.name) / "owner.att-terminal-no-session.claude.jsonl"
        missing.write_text(json.dumps({"type": "dispatch.supervisor.started"}) + "\n"
                           + json.dumps({"type": "result", "result": "PASS"}) + "\n")
        decided, announced = S.announced_session(missing)
        self.assertTrue(decided)
        self.assertEqual(announced["invalid"], "missing-supervisor-session-event")

    def test_late_transcript_file_is_re_resolved_without_caching_absence(self):
        from claude_project_dir import encode_project_dir
        session_id = "11111111-2222-3333-4444-555555555555"
        log = Path(self.tmp.name) / "owner.att-late-file.claude.jsonl"
        log.write_text(self._announce(session_id, "/late-worktree"), encoding="utf-8")
        home = Path(self.tmp.name) / "late-worker-home"
        cache = {}
        self.assertIsNone(S._summary_source(log, cache, runtime_home=home,
                                            attempt_cwd="/late-worktree"))
        self.assertNotIn("source", cache)
        source = home / "projects" / encode_project_dir("/late-worktree") / (session_id + ".jsonl")
        source.parent.mkdir(parents=True)
        source.write_text("late conversation", encoding="utf-8")
        self.assertEqual(S._summary_source(log, cache, runtime_home=home,
                                           attempt_cwd="/late-worktree"), source)

    def test_one_shot_log_stays_its_own_summary_source(self):
        log = Path(self.tmp.name) / "owner.att-oneshot.claude.jsonl"
        log.write_text(
            json.dumps({"type": "assistant", "message": {"content": [
                {"type": "text", "text": "streamed turn"}]}}) + "\n",
            encoding="utf-8",
        )
        decided, announced = S.announced_session(log)
        self.assertTrue(decided)
        self.assertIsNone(announced)
        self.assertEqual(S._summary_source(log, {}), log)

    def _opencode_source(self, attempt="att-native-now"):
        log = Path(self.tmp.name) / f"owner.{attempt}.opencode.jsonl"
        log.write_text(json.dumps({"type":"dispatch.supervisor.continuation-budget"})+"\n"+
            json.dumps({"type":"dispatch.supervisor.turn-started","parent_attempt_id":attempt})+"\n"+
            json.dumps({"type":"dispatch.supervisor.session", "runtime":"opencode",
            "session_id":"ses_exact", "cwd":"/wt", "parent_attempt_id":attempt})+"\n")
        runtime = Path(self.tmp.name) / "runtime"
        db = runtime / "data/opencode/opencode.db"
        db.parent.mkdir(parents=True)
        con = sqlite3.connect(db)
        con.execute("CREATE TABLE session(id TEXT, directory TEXT, time_created INTEGER, time_updated INTEGER)")
        con.execute("INSERT INTO session VALUES(?,?,?,?)", ("ses_exact", "/wt", 1, 1))
        con.execute("CREATE TABLE part(session_id TEXT,data TEXT)")
        con.execute("INSERT INTO part VALUES(?,?)", ("ses_exact",json.dumps({
            "type":"text","text":"Preparing the exact owner continuation"})))
        con.commit()
        con.close()
        jobs = Path(self.tmp.name) / "native.jobs.log"
        jobs.write_text("2026-10-09T00:00:00Z\topen\t/repo\t/wt\towner\t"+
            f"attempt_id={attempt},harness=opencode,log_file={log},opencode_runtime_dir={runtime}\n")
        os.environ["AGENT_DISPATCH_JOBS"] = str(jobs)
        return log, db, jobs

    def test_opencode_announcement_selects_exact_private_db(self):
        log, db, jobs = self._opencode_source()
        cache = {}
        with mock.patch.object(S,"session_transcript",side_effect=AssertionError("Claude lookup")):
            self.assertEqual(S._summary_source(log,cache),db)
        self.assertEqual(cache["refresh_source"], {"kind":"opencode-db", "db_path":str(db),
                                                 "session_id":"ses_exact"})
        jobs.write_text(jobs.read_text().replace("att-native-now","att-foreign"))
        self.assertIsNone(S._summary_source(log,{}))

    def test_opencode_summary_sid_stays_attempt_scoped(self):
        log, db, _ = self._opencode_source()
        cache = {}
        S._summary_source(log,cache)
        from fleet import refresh_title
        with mock.patch.object(refresh_title,"maybe_spawn",return_value=True) as spawn:
            self.assertTrue(S._refresh("opencode","dispatch-att-native-now",db,phase="initial",
                debounce=0,priority=True,refresh_source=cache["refresh_source"]))
        self.assertEqual(spawn.call_args.kwargs["sid"],"dispatch-att-native-now")
        self.assertEqual(spawn.call_args.kwargs["refresh_source"]["session_id"],"ses_exact")

    def test_opencode_native_db_produces_attempt_now_without_claude_transcript(self):
        log, db, _ = self._opencode_source()
        import opencode_runtime
        observation = opencode_runtime.read_session_observation("ses_exact", db_path=db)
        self.assertEqual((observation["state"], observation["generation"]), ("observed", "v1"))
        self.assertEqual(observation["session"]["directory"], "/wt")
        worker = subprocess.Popen(["sleep","0.5"],start_new_session=True)
        try:
            start = S.process_observation(worker.pid)[1]
            rc = S.supervise(attempt_id="att-native-now",harness="opencode",transcript=log,
                target_pid=worker.pid,target_start=start,poll=.05,initial_delay=0,
                final_grace=8,log_quiet=.05)
            self.assertEqual(rc,0)
            sidecar = titles.read("dispatch-att-native-now",harness="opencode")
            self.assertEqual(sidecar["summary"],"분사 작업 요약을 갱신하고 있습니다")
            self.assertTrue(sidecar["cursor_kind"].startswith("opencode-rowid-v1:"))
        finally:
            worker.wait(timeout=3)

    def test_empty_log_is_undecided_and_never_cached_as_unsupervised(self):
        """The summary owner starts before the worker clears its launch fence."""
        log = Path(self.tmp.name) / "owner.att-race.claude.jsonl"
        log.write_text("", encoding="utf-8")
        self.assertEqual(S.announced_session(log), (False, None))
        cache: dict = {}
        self.assertIsNone(S._summary_source(log, cache))
        self.assertNotIn("source", cache)
        self.assertNotIn("announced", cache)
        # The announcement lands on a later poll and must still be honoured.
        session_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        projects = Path(self.tmp.name) / "config" / "projects" / "-wt"
        projects.mkdir(parents=True)
        (projects / f"{session_id}.jsonl").write_text("{}\n", encoding="utf-8")
        log.write_text(self._announce(session_id, "/wt"), encoding="utf-8")
        os.environ["CLAUDE_CONFIG_DIR"] = str(Path(self.tmp.name) / "config")
        self.assertEqual(
            S._summary_source(log, cache), projects / f"{session_id}.jsonl")

    def test_announced_transcript_resolves_by_exact_session_id(self):
        session_id = "99999999-8888-7777-6666-555555555555"
        config = Path(self.tmp.name) / "config"
        os.environ["CLAUDE_CONFIG_DIR"] = str(config)
        # Only the exact encoded cwd in the selected home is eligible.
        from claude_project_dir import encode_project_dir
        cwd = "/home/user/agent_note.wt/hook_sweep"
        encoded = config / "projects" / encode_project_dir(cwd)
        encoded.mkdir(parents=True)
        exact = encoded / f"{session_id}.jsonl"
        exact.write_text("{}\n", encoding="utf-8")
        self.assertEqual(
            S.session_transcript(
                {"session_id": session_id, "cwd": cwd}, expected_cwd=cwd,
                runtime_home=config),
            exact,
        )
        # A same-id transcript under a different project is never borrowed.
        self.assertIsNone(S.session_transcript(
            {"session_id": session_id, "cwd": "/somewhere/else"},
            expected_cwd="/somewhere/else", runtime_home=config))
        # A missing transcript never borrows a same-dir neighbour.
        self.assertIsNone(
            S.session_transcript({"session_id": "00000000-0000-0000-0000-000000000000",
                                  "cwd": "/home/user/agent_note.wt/hook_sweep"}))

    def test_supervised_owner_gets_a_sidecar_from_the_announced_transcript(self):
        """End-to-end: announcement -> child transcript -> title and NOW line.

        This is the path that produced no summary at all: the receipt log alone
        carries no model text, so the sidecar has to come from the child's own
        transcript for a supervised owner to be nameable in Fleet.
        """
        attempt = "att-supervised-e2e"
        session_id = "abcdabcd-1234-5678-9abc-abcdabcdabcd"
        producer_home = Path(self.tmp.name) / "producer-home"
        dispatch_root = Path(self.tmp.name) / "dispatch-state"
        jobs = dispatch_root / "jobs.log"
        dispatch_root.mkdir()
        from worker_runtime_home import _builder
        home_key = _builder.worker_home_key(str(ROOT), "claude", "stage", attempt)
        worker_home = dispatch_root / "homes" / "workers" / home_key
        os.environ["CLAUDE_CONFIG_DIR"] = str(producer_home)
        # The isolated suite runner intentionally removes ambient dispatch
        # state. Model the real registered launch in this test's private root:
        # HOME-scoped reads need its jobs path and managed HOME identity.
        os.environ["AGENT_DISPATCH_JOBS"] = str(jobs)
        from claude_project_dir import encode_project_dir
        project = worker_home / "projects" / encode_project_dir("/wt/owner")
        decoy_project = producer_home / "projects" / encode_project_dir("/wt/owner")
        project.mkdir(parents=True)
        decoy_project.mkdir(parents=True)
        (worker_home / "worker-home.json").write_text(json.dumps({
            "schema": 2,
            "identity": attempt,
            "key": home_key,
            "harness": "claude",
            "worker_type": "stage",
            "source": str(ROOT),
        }), encoding="utf-8")
        transcript = project / f"{session_id}.jsonl"
        (decoy_project / f"{session_id}.jsonl").write_text(
            json.dumps({"type": "assistant", "message": {"content": [
                {"type": "text", "text": "PRODUCER_HOME_DECOY"}]}}) + "\n",
            encoding="utf-8")
        transcript.write_text(json.dumps({
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "WORKER_HOME_ONLY owner turn one"}]},
        }) + "\n", encoding="utf-8")
        provider_script = (
            "import sys; p=sys.argv[-1]; "
            "assert 'WORKER_HOME_ONLY' in p and 'PRODUCER_HOME_DECOY' not in p; "
            "print('TITLE: 작업 홈 요약\\nNOW: worker home conversation')"
        )
        os.environ["FLEET_TITLE_COMMAND"] = shlex.join([sys.executable, "-c", provider_script])
        log = Path(self.tmp.name) / f"owner.{attempt}.claude.jsonl"
        log.write_text(self._announce(session_id, "/wt/owner"), encoding="utf-8")
        jobs.write_text(
            "2026-10-19T00:00:00Z\topen\t/wt/owner\t/wt/owner\towner\t"
            "attempt_schema_version=2,dispatch_depth=1,transport=headless,"
            "execution_surface=registered-headless,registered_worker=1,"
            "fallback_hop=same-harness-headless,worker_type=stage,"
            f"attempt_id={attempt},harness=claude,runtime_home={worker_home},log_file={log}\n",
            encoding="utf-8",
        )
        child = subprocess.Popen([
            sys.executable, "-c",
            "import pathlib,sys,time,json; time.sleep(.4); "
            "p=pathlib.Path(sys.argv[1]); f=p.open('a'); "
            "f.write(json.dumps({'type':'assistant','message':{'content':"
            "[{'type':'text','text':'WORKER_HOME_ONLY owner turn two'}]}})+'\\n'); f.close(); time.sleep(.25)",
            str(transcript),
        ], start_new_session=True)
        start = S.process_observation(child.pid)[1]
        self.assertTrue(start)
        rc = S.supervise(
            attempt_id=attempt, harness="claude", transcript=log,
            target_pid=child.pid, target_start=start,
            poll=0.05, initial_delay=0, periodic_debounce=90,
            final_grace=8, log_quiet=0.05,
            runtime_home=worker_home, attempt_cwd="/wt/owner",
        )
        child.wait(timeout=5)
        self.assertEqual(rc, 0)
        sidecar = titles.read(S.summary_sid(attempt), harness="claude")
        self.assertEqual(sidecar["title"], "작업 홈 요약")
        self.assertEqual(sidecar["summary"], "worker home conversation")
        from types import SimpleNamespace
        from fleet import render
        card = render._context_detail_row(SimpleNamespace(
            summary=sidecar["summary"], summary_ts=sidecar["summary_ts"],
            liveness="working", _details_pending=False, _details_error=False),
            term_width=168)
        self.assertIn("worker home conversation", render._plain(card[0]))
        # The cursor tracks the transcript, never the receipt log.
        self.assertEqual(sidecar["offset"], transcript.stat().st_size)
        self.assertNotEqual(sidecar["offset"], log.stat().st_size)
        state = json.loads(S.owner_state_path("claude", attempt).read_text())
        self.assertEqual(state["summary_source"], str(transcript))
        self.assertEqual(state["status"], "terminal")

    def test_supervisor_announces_its_child_session_before_the_first_turn(self):
        source = (ROOT / "utilities" / "claude-session-supervisor.py").read_text()
        announce = source.index('"type": "dispatch.supervisor.session"')
        first_turn = source.index("result, process_rc = run_turn(")
        self.assertLess(announce, first_turn)

    def test_all_three_wrappers_attach_owner_at_pre_release_boundary(self):
        for harness in ("claude", "codex", "opencode"):
            source = (ROOT / "adapters" / harness / "bin" / "dispatch-headless.py").read_text()
            self.assertIn("from dispatch_summary import launch_summary_owner", source)
            self.assertIn("DISPATCH_MAIN.launch_claimed_attempt(", source)
            self.assertIn(f'harness="{harness}"', source)
        shared = (ROOT / "utilities" / "dispatch_headless_main.py").read_text()
        self.assertIn("pre_release=lambda identity: h.attach_summary_owner", shared)
        opencode = (ROOT / "adapters" / "opencode" / "bin" / "dispatch-headless.py").read_text()
        self.assertIn("args.command_attempt_id = args.attempt_id", opencode)

    def test_review_flock_spans_exact_target_lifetime_across_processes(self):
        attempt = "att-review-governed"
        cycle = "cyc-review-governed"
        nonce = "b" * 64
        root = Path(self.tmp.name) / ".agent_reports"
        root.mkdir()
        log = Path(self.tmp.name) / f"review.{attempt}.codex.jsonl"
        log.write_text("", encoding="utf-8")
        target = subprocess.Popen(
            [sys.executable, "-c", "import sys; sys.stdin.buffer.read(1)"],
            stdin=subprocess.PIPE,
            start_new_session=True,
        )
        summary_owners = []
        real_popen = subprocess.Popen

        def track_summary_owner(*args, **kwargs):
            process = real_popen(*args, **kwargs)
            summary_owners.append(process)
            return process

        try:
            start = S.process_observation(target.pid)[1]
            with mock.patch.object(
                S.subprocess, "Popen", side_effect=track_summary_owner
            ):
                receipt = S.launch_summary_owner(
                    attempt_id=attempt, harness="codex", transcript=log,
                    target_pid=target.pid, target_start=start,
                    review_artifact_root=root, review_cycle_id=cycle,
                    review_lease_nonce=nonce,
                )
            self.assertEqual(len(summary_owners), 1)
            self.assertEqual(
                summary_owners[0].pid, int(receipt["summary_owner_pid"])
            )
            metadata = {
                "attempt_id": attempt, "review_cycle_id": cycle,
                "review_governed_lease": D.REVIEW_GOVERNED_LEASE_KIND,
                "review_governed_lease_nonce": nonce,
            }
            self.assertTrue(D.review_governed_lease_is_held(root, metadata))
            self.assertIsNone(target.poll())
            target.stdin.write(b"release\n")
            target.stdin.flush()
            target.stdin.close()
            target.wait(timeout=5)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if not D.review_governed_lease_is_held(root, metadata):
                    break
                time.sleep(0.05)
            self.assertFalse(D.review_governed_lease_is_held(root, metadata))
        finally:
            if target.stdin is not None and not target.stdin.closed:
                try:
                    target.stdin.close()
                except BrokenPipeError:
                    pass
            for process in (target, *summary_owners):
                if process.poll() is None:
                    try:
                        os.killpg(process.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    process.wait(timeout=5)


class ProviderLockdirReferenceTest(unittest.TestCase):
    """`_provider_lockdir_reference` over a fake /proc tree."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.proc = Path(self._tmp.name) / "proc"
        self.proc.mkdir()
        self.lockdir = Path(self._tmp.name) / "titles" / ".lock-sid"
        self.lockdir.mkdir(parents=True)

    def process(self, pid, argv, *, uids=None, state="S (sleeping)", status=True, cmdline=True,
                mm=True, uid_line=True, mount_ns=None):
        base = self.proc / str(pid)
        base.mkdir()
        me = os.geteuid()
        uids = uids if uids is not None else (me, me, me, me)
        # A directory stands in for a file that exists but cannot be read (EISDIR).
        if status:
            (base / "status").write_text(
                f"Name:\tx\nState:\t{state}\n"
                + ("Uid:\t" + "\t".join(map(str, uids)) + "\n" if uid_line else "")
                + ("VmSize:\t1000 kB\n" if mm else ""))
        else:
            (base / "status").mkdir()
        if cmdline:
            (base / "cmdline").write_bytes(("\0".join(argv) + "\0").encode() if argv else b"")
        else:
            (base / "cmdline").mkdir()
        if mount_ns is not None:
            (base / "ns").mkdir()
            (base / "ns" / "mnt").symlink_to(mount_ns)

    def reference(self):
        return S._provider_lockdir_reference(self.lockdir, self.proc)

    def test_a_process_naming_the_lockdir_references_it(self):
        self.process(10, ["python3", "refresh", "--lockdir", str(self.lockdir)])
        self.assertEqual(self.reference()[0], "referenced")

    def test_nothing_naming_it_is_unreferenced(self):
        self.process(10, ["python3", "refresh", "--lockdir", str(self.lockdir) + "-other"])
        self.process(11, ["sleep", "30"])
        self.assertEqual(self.reference(), ("unreferenced", ""))

    def test_unreadable_status_is_unknown_but_a_vanished_process_is_not(self):
        self.process(10, ["sleep"], status=False)
        self.assertEqual(self.reference()[0], "unknown")
        (self.proc / "10" / "status").rmdir()  # ENOENT: the process is gone
        self.assertEqual(self.reference()[0], "unreferenced")

    def test_same_user_live_process_with_empty_cmdline_is_unknown(self):
        self.process(10, [])
        self.assertEqual(self.reference(), ("unknown", "cmdline-empty"))

    def test_unreadable_cmdline_is_unknown(self):
        self.process(10, ["sleep"], cmdline=False)
        self.assertEqual(self.reference(), ("unknown", "cmdline-unreadable"))

    def test_zombie_and_other_user_are_ignored(self):
        self.process(10, [], state="Z (zombie)")
        self.process(11, [], uids=(os.geteuid() + 1,) * 4)
        self.assertEqual(self.reference(), ("unreferenced", ""))

    def test_effective_uid_alone_makes_a_candidate(self):
        me = os.geteuid()
        self.process(10, [], uids=(me + 1, me, me + 1, me + 1))
        self.assertEqual(self.reference()[0], "unknown")

    def test_exiting_process_without_address_space_holds_no_argv(self):
        # In do_exit: not yet a zombie, memory already released, cmdline empty.
        self.process(10, [], mm=False)
        self.assertEqual(self.reference(), ("unreferenced", ""))

    def test_status_without_uid_line_is_incomplete_not_another_user(self):
        self.process(10, ["sleep"], uid_line=False)
        self.assertEqual(self.reference(), ("unknown", "status-incomplete"))

    def test_holder_in_another_mount_view_is_found_through_its_root(self):
        (self.proc / "self" / "ns").mkdir(parents=True)
        (self.proc / "self" / "ns" / "mnt").symlink_to("mnt:[1]")
        self.process(10, ["x", "--lockdir", "/their/view/.lock-sid"], mount_ns="mnt:[2]")
        root = self.proc / "10" / "root"
        (root / "their").mkdir(parents=True)
        self.assertEqual(self.reference()[0], "unreferenced")  # their path does not resolve here
        (root / "their" / "view").symlink_to(self.lockdir.parent)
        self.assertEqual(self.reference(), ("referenced", "argv-mount-view"))

    def test_same_mount_view_is_not_resolved_through_root(self):
        mine = "mnt:[1]"
        (self.proc / "self" / "ns").mkdir(parents=True)
        (self.proc / "self" / "ns" / "mnt").symlink_to(mine)
        self.process(10, ["x", "--lockdir", "/their/view/.lock-sid"], mount_ns=mine)
        (self.proc / "10" / "root").mkdir()
        (self.proc / "10" / "root" / "their").mkdir()
        (self.proc / "10" / "root" / "their" / "view").symlink_to(self.lockdir.parent)
        self.assertEqual(self.reference(), ("unreferenced", ""))

    def test_unresolvable_root_of_a_lockdir_holder_is_unknown(self):
        (self.proc / "self" / "ns").mkdir(parents=True)
        (self.proc / "self" / "ns" / "mnt").symlink_to("mnt:[1]")
        # The mount namespace cannot be read, and neither can the root: a file
        # in place of the directory stands for EACCES-style refusal via ELOOP.
        self.process(10, ["x", "--lockdir", "/their/view/.lock-sid"])
        (self.proc / "10" / "root").symlink_to("root")  # loops onto itself: ELOOP
        self.assertEqual(self.reference(), ("unknown", "token-unreadable"))

    def test_process_without_lockdir_gets_no_root_or_namespace_read(self):
        self.process(10, ["sleep", "30"])
        with mock.patch.object(S.os, "readlink", side_effect=AssertionError("read")):
            tokens, reason = S._walk_provider_argv(self.proc)
        self.assertEqual((tokens, reason), ((), ""))

    def test_listing_limit_and_missing_proc_are_unknown(self):
        self.process(10, ["sleep"])
        with mock.patch.object(S, "MAX_PROVIDER_SCAN_PIDS", 0):
            self.assertEqual(self.reference(), ("unknown", "proc-limit"))
        self.assertEqual(S._provider_lockdir_reference(
            self.lockdir, self.proc / "missing")[0], "unknown")

    def test_scope_walks_the_real_table_once_for_every_lockdir(self):
        with mock.patch.object(S, "_walk_provider_argv", return_value=((), "")) as walk, \
                S.provider_reference_scan_scope():
            S._provider_lockdir_reference(self.lockdir)
            S._provider_lockdir_reference(self.lockdir)
            with S.provider_reference_scan_scope():
                S._provider_lockdir_reference(self.lockdir)
        self.assertEqual(walk.call_count, 1)
        with mock.patch.object(S, "_walk_provider_argv", return_value=((), "")) as walk:
            S._provider_lockdir_reference(self.lockdir)
            S._provider_lockdir_reference(self.lockdir)
        self.assertEqual(walk.call_count, 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
