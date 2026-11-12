#!/usr/bin/env python3
import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from route_identity import route_hash


ROOT = Path(__file__).resolve().parents[1]
WATCH = ROOT / "utilities" / "dispatch-orphan-watch.py"


class OrphanWatchTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.home = self.base / "home"
        self.jobs = self.base / "jobs.log"
        self.route_id = "rt-watch"
        self.route = self.base / "route.json"
        route = {
            "route_id": self.route_id,
            "nodes": [
                {"id": "plan", "depends_on": []},
                {"id": "execute", "depends_on": ["plan"]},
            ],
        }
        route["route_hash"] = route_hash(route)
        self.route.write_text(json.dumps(route))
        marker_dir = self.home / ".dispatch" / "completion" / self.route_id
        marker_dir.mkdir(parents=True)
        (marker_dir / "plan.json").write_text(json.dumps({"node_id": "plan"}))
        self.supervisor_state = (
            self.jobs.parent / "supervisor-state" / "att-watch.json"
        )
        self.supervisor_state.parent.mkdir(parents=True)
        self.supervisor_state.write_text("{}", encoding="utf-8")
        self.supervisor_lease = (
            self.jobs.parent / "supervisor-state" / "att-watch.lease"
        )
        self.supervisor_lease.parent.mkdir(parents=True, exist_ok=True)
        self.supervisor_lease.write_text("stale", encoding="utf-8")
        self.owner = subprocess.Popen(["sleep", "60"], start_new_session=True)
        self.owner_start = self.proc_start(self.owner.pid)
        self.observer_namespace = (Path("/proc/self/ns/pid").readlink())
        self.children = []

    def tearDown(self):
        if self.owner.poll() is None:
            self.owner.kill()
        self.owner.wait()
        for child in self.children:
            if child.poll() is None:
                child.kill()
            child.wait()
        self.temp.cleanup()

    @staticmethod
    def proc_start(pid):
        raw = (Path("/proc") / str(pid) / "stat").read_text()
        return raw[raw.rfind(")") + 2:].split()[19]

    def write_rows(self, completed_owner=False, child=None, supervised=True):
        status = "done" if completed_owner else "open"
        child_pid = child.pid if child is not None else 99999999
        child_start = self.proc_start(child.pid) if child is not None else self.owner_start
        child_group = f",pgid={child_pid},pid_observer_ns={self.observer_namespace},pid_ns={self.observer_namespace}"
        current = (
            "attempt_schema_version=2,transport=headless,"
            "execution_surface=registered-headless,registered_worker=1,"
            "fallback_hop=same-harness-headless"
        )
        supervised_metadata = (
            ",harness=codex,completion_delivery=app-server-supervised,"
            "supervisor_lease=flock-v1,"
            f"supervisor_lease_file={self.supervisor_lease}"
            if supervised
            else ""
        )
        self.jobs.write_text(
            f"2026-07-19T00:00:00Z\t{status}\t/r\t/w\towner\t"
            f"{current},dispatch_depth=1,worker_type=owner,attempt_id=att-watch,"
            "parent_sid=parent-test,parent_completion_delivery=claude-parent-runtime,"
            f"pid={self.owner.pid},pid_start={self.owner_start},pgid={self.owner.pid},"
            f"pid_observer_ns={self.observer_namespace},"
            f"pid_ns={self.observer_namespace}"
            f"{supervised_metadata}\n"
            "2026-07-19T00:00:01Z\topen\t/r\t/w\tchild\t"
            f"{current},dispatch_depth=2,route_id={self.route_id},"
            f"route_file={self.route},route_hash={json.loads(self.route.read_text())['route_hash']},"
            "route_node=execute,attempt_id=att-child,"
            f"parent=owner,parent_attempt_id=att-watch,pid={child_pid},"
            f"pid_start={child_start}{child_group}\n"
        )

    def watcher(self):
        process = subprocess.Popen([
            sys.executable, str(WATCH),
            "--jobs", str(self.jobs),
            "--agent-home", str(self.home),
            "--attempt-id", "att-watch",
            "--pid", str(self.owner.pid),
            "--pid-start", self.owner_start,
            "--interval", "0.02",
        ])
        def reap():
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=5)
        self.addCleanup(reap)
        return process

    def test_owner_exit_reaps_exact_child_group_and_closes_both_rows(self):
        child = subprocess.Popen(["sleep", "60"], start_new_session=True)
        self.children.append(child)
        self.write_rows(child=child)
        watcher = self.watcher()
        time.sleep(0.05)
        self.owner.kill(); self.owner.wait()
        self.assertEqual(watcher.wait(timeout=5), 0)
        text = self.jobs.read_text()
        self.assertIn("\tdone\t/r\t/w\towner\t", text)
        self.assertIn("note=dead-parent-orphaned", text)
        self.assertIn("\tdone\t/r\t/w\tchild\t", text)
        self.assertIn("note=dead-parent-terminated", text)
        self.assertIsNotNone(child.poll())
        self.assertFalse(self.supervisor_state.exists())
        self.assertFalse(self.supervisor_lease.exists())

    def test_owner_exit_before_any_child_closes_the_open_row(self):
        self.write_rows(supervised=False)
        lines = self.jobs.read_text().splitlines()
        self.jobs.write_text(lines[0] + "\n")
        watcher = self.watcher()
        time.sleep(0.05)
        self.owner.kill(); self.owner.wait()
        self.assertEqual(watcher.wait(timeout=5), 0)
        text = self.jobs.read_text()
        self.assertIn("\tdone\t/r\t/w\towner\t", text)
        self.assertIn("note=dead-exact-pid", text)
        self.assertFalse(self.supervisor_state.exists())
        self.assertTrue(self.supervisor_lease.exists())

    def test_terminal_owner_waits_then_finishes_child_cleanup(self):
        self.write_rows(completed_owner=True)
        self.route.with_suffix(".outcome.json").write_text(json.dumps({
            "route_id": self.route_id,
            "route_hash": json.loads(self.route.read_text())["route_hash"],
            "terminal_gate_proven": True,
        }))
        before = self.jobs.read_text()
        watcher = self.watcher()
        time.sleep(0.08)
        self.assertIsNone(watcher.poll())
        self.assertTrue(self.supervisor_state.exists())
        self.owner.kill(); self.owner.wait()
        self.assertEqual(watcher.wait(timeout=5), 0)
        after = self.jobs.read_text()
        self.assertEqual(after.splitlines()[0], before.splitlines()[0])
        self.assertIn("\tdone\t/r\t/w\tchild\t", after)
        self.assertFalse(self.supervisor_state.exists())
        self.assertFalse(self.supervisor_lease.exists())

    def test_fleet_kill_waits_then_closes_exact_child_and_protects_foreign_sibling(self):
        child = subprocess.Popen(["sleep", "60"], start_new_session=True)
        sibling = subprocess.Popen(["sleep", "60"], start_new_session=True)
        self.children.extend((child, sibling))
        self.write_rows(completed_owner=True, child=child)
        current = (
            "attempt_schema_version=2,transport=headless,execution_surface=registered-headless,"
            "registered_worker=1,fallback_hop=same-harness-headless"
        )
        self.jobs.write_text(self.jobs.read_text() +
            "2026-07-19T00:00:02Z\topen\t/r\t/w\tforeign\t"
            f"{current},dispatch_depth=2,route_id={self.route_id},route_node=test,"
            f"attempt_id=att-foreign,parent=other,parent_attempt_id=att-other,"
            f"pid={sibling.pid},pid_start={self.proc_start(sibling.pid)},"
            "pid_scope=namespace-local,launch_lifecycle=foreground-scoped\n")
        watcher = self.watcher()
        time.sleep(0.08)
        self.assertIsNone(watcher.poll())
        self.assertIsNone(child.poll())
        self.owner.kill(); self.owner.wait()
        self.assertEqual(watcher.wait(timeout=5), 0)
        text = self.jobs.read_text()
        self.assertIn("att-child", text)
        child_line = next(line for line in text.splitlines() if "att-child" in line)
        sibling_line = next(line for line in text.splitlines() if "att-foreign" in line)
        self.assertIn("note=dead-parent-terminated", child_line)
        self.assertIn("\topen\t", sibling_line)
        self.assertIsNotNone(child.poll())
        self.assertIsNone(sibling.poll())

    def test_killed_owner_also_waits_for_extinction_before_namespace_close(self):
        self.write_rows(completed_owner=True)
        text = self.jobs.read_text()
        text = text.replace("\tdone\t/r\t/w\towner\t", "\tkilled\t/r\t/w\towner\t")
        text = text.replace(
            f"pid_start={self.owner_start},pgid={self.owner.pid},",
            f"pid_start={self.owner_start},pgid={self.owner.pid},pid_scope=namespace-local,",
            1,
        )
        text = text.replace(
            "pid_start=1\n",
            "pid_start=1,pid_scope=namespace-local,"
            "launch_lifecycle=foreground-scoped\n",
        )
        self.jobs.write_text(text)
        watcher = self.watcher()
        time.sleep(0.08)
        self.assertIsNone(watcher.poll())
        child_line = next(line for line in self.jobs.read_text().splitlines()
                          if "att-child" in line)
        self.assertIn("\topen\t", child_line)
        self.owner.kill(); self.owner.wait()
        self.assertEqual(watcher.wait(timeout=5), 70)
        self.assertTrue(self.supervisor_state.exists())
        notices = list((self.jobs.parent / "pending-delivery").glob("**/delivery-*.json"))
        self.assertEqual(len(notices), 1)
        self.assertEqual(json.loads(notices[0].read_text())["receipt"]["reason"], "supervisor-exited")
        child_line = next(line for line in self.jobs.read_text().splitlines()
                          if "att-child" in line)
        self.assertIn("note=dead-parent-exited", child_line)

    def test_non_orphan_429_exit_is_reconciled_as_capacity(self):
        log = self.base / "owner.claude.jsonl"
        log.write_text(json.dumps({
            "type": "result",
            "subtype": "error_during_execution",
            "is_error": True,
            "api_error_status": 429,
            "result": "You've reached your Fable 5 limit",
        }) + "\n", encoding="utf-8")
        current = (
            "attempt_schema_version=2,dispatch_depth=1,transport=headless,"
            "execution_surface=registered-headless,registered_worker=1,"
            "fallback_hop=same-harness-headless,worker_type=owner,"
            "harness=claude"
        )
        self.jobs.write_text(
            "2026-07-19T00:00:00Z\topen\t/r\t/w\towner\t"
            f"{current},attempt_id=att-watch,pid={self.owner.pid},"
            f"pid_start={self.owner_start},pgid={self.owner.pid},log_file={log}\n"
        )
        watcher = self.watcher()
        time.sleep(0.05)
        self.owner.kill(); self.owner.wait()
        self.assertEqual(watcher.wait(timeout=5), 0)
        registry = self.jobs.read_text(encoding="utf-8")
        self.assertIn("\tdone\t/r\t/w\towner\t", registry)
        self.assertIn("note=dead-capacity", registry)
        self.assertIn("failure_class=capacity", registry)
        self.assertTrue(self.supervisor_lease.exists())

    def test_non_orphan_signal_exit_without_envelope_is_still_closed(self):
        current = (
            "attempt_schema_version=2,dispatch_depth=1,transport=headless,"
            "execution_surface=registered-headless,registered_worker=1,"
            "fallback_hop=same-harness-headless,worker_type=owner,harness=codex"
        )
        self.jobs.write_text(
            "2026-07-19T00:00:00Z\topen\t/r\t/w\towner\t"
            f"{current},attempt_id=att-watch,pid={self.owner.pid},"
            f"pid_start={self.owner_start},pgid={self.owner.pid}\n"
        )
        watcher = self.watcher()
        time.sleep(0.05)
        self.owner.kill(); self.owner.wait()
        self.assertEqual(watcher.wait(timeout=5), 0)
        registry = self.jobs.read_text(encoding="utf-8")
        self.assertIn("\tdone\t/r\t/w\towner\t", registry)
        self.assertIn("note=dead-protocol", registry)
        self.assertTrue(self.supervisor_lease.exists())


def load_watch_module():
    import importlib.util
    spec = importlib.util.spec_from_file_location("dispatch_orphan_watch_unit", WATCH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class IdleOwnerWatchTest(unittest.TestCase):
    """A live owner is watched by PID; the registry is judged only on change."""

    ROW = ("2026-07-19T00:00:00Z\topen\t/r\t/w\towner\t"
           "attempt_schema_version=2,dispatch_depth=1,worker_type=owner,attempt_id=att-idle{extra}\n")

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.jobs = Path(self.temp.name) / "jobs.log"
        self.jobs.write_text("other\trow\n" * 50 + self.ROW.format(extra=""), encoding="utf-8")
        self.watch = load_watch_module()
        self.now = [100.0]
        real_record = self.watch.attempt_record
        self.records = mock.patch.object(self.watch, "attempt_record", side_effect=real_record)
        self.reads = self.records.start()
        self.addCleanup(self.records.stop)
        self.close = mock.patch("route_parent_close.row_requested",
                                side_effect=lambda meta, jobs: meta.get("parent_close_requested") == "1")
        self.judged = self.close.start()
        self.addCleanup(self.close.stop)
        clock = mock.patch.object(self.watch.time, "monotonic", side_effect=lambda: self.now[0])
        clock.start()
        self.addCleanup(clock.stop)

    def rewrite(self, text):
        # A rewrite with a new inode, as the registry writer replaces it.
        replacement = self.jobs.with_suffix(".next")
        replacement.write_text(text, encoding="utf-8")
        replacement.replace(self.jobs)

    def test_registry_refresh_keeps_the_passive_judgment_inside_one_read_scope(self):
        import dispatch_registry_cache
        import route_parent_close
        registry = self.watch.RegistryObservation(self.jobs, "att-idle")
        observed = []
        with mock.patch.object(
                route_parent_close, "row_requested",
                side_effect=lambda *_args: observed.append(
                    dispatch_registry_cache.registry_scope_active()) or False):
            registry.refresh(force=True)
        self.assertEqual(observed, [True])

    def test_unchanged_registry_is_not_reparsed_and_close_intent_has_a_backstop(self):
        registry = self.watch.RegistryObservation(self.jobs, "att-idle")
        for _ in range(10):
            self.assertEqual(registry.refresh()[0], "open")
            self.now[0] += 2.0
        self.assertEqual(self.reads.call_count, 1)
        # 20 s idle: only the first judgment.
        self.assertEqual(self.judged.call_count, 1)
        self.now[0] += self.watch.CLOSE_INTENT_BACKSTOP_SECONDS
        registry.refresh()
        self.assertEqual((self.reads.call_count, self.judged.call_count), (1, 2))

        # Another row changed: the watched row is re-read, not re-judged.
        self.rewrite("other\tchanged\n" + self.jobs.read_text(encoding="utf-8"))
        registry.refresh()
        self.assertEqual((self.reads.call_count, self.judged.call_count), (2, 2))

        # The watched row itself carries a parent close: judged at once.
        self.rewrite(self.ROW.format(extra=",parent_close_requested=1"))
        status, _metadata, close_requested = registry.refresh()
        self.assertEqual((status, close_requested), ("open", True))
        self.assertEqual((self.reads.call_count, self.judged.call_count), (3, 3))

    def test_watch_reacts_to_parent_close_and_owner_exit_without_polling_registry(self):
        args = SimpleNamespace(jobs=self.jobs, attempt_id="att-idle", pid=4242,
                               pid_start="1", interval=2.0)
        sleeps = []

        def sleep(seconds):
            sleeps.append(seconds)
            self.now[0] += seconds
            if len(sleeps) == 20:
                self.rewrite(self.ROW.format(extra=",parent_close_requested=1"))

        with mock.patch.object(self.watch, "process_start", return_value="1"), \
             mock.patch.object(self.watch.time, "sleep", side_effect=sleep), \
             mock.patch("route_parent_close.recover_attempt",
                             return_value={"state": "cancelled"}) as recover, \
             mock.patch.object(self.watch, "_remove_supervisor_state") as removed:
            self.assertEqual(self.watch.watch(args), 0)
        recover.assert_called_once()
        removed.assert_called_once()
        # 40 s with a live owner: one row read and one backstop judgment
        # before the parent close arrived, instead of 20 of each.
        self.assertEqual((self.reads.call_count, self.judged.call_count), (2, 3))

        self.reads.reset_mock()
        self.judged.reset_mock()
        self.rewrite(self.ROW.format(extra=""))
        with mock.patch.object(self.watch, "process_start", side_effect=["1"] * 5 + ["2"]), \
             mock.patch.object(self.watch.time, "sleep", side_effect=sleep), \
             mock.patch.object(self.watch, "observed_owner_lifecycle",
                                    return_value=(None, "", {})), \
             mock.patch.object(self.watch, "reconcile_exact_exit", return_value=0) as exited:
            self.assertEqual(self.watch.watch(args), 0)
        exited.assert_called_once_with(args)
        self.assertEqual((self.reads.call_count, self.judged.call_count), (1, 1))


class SharedRunnerWakeTest(unittest.TestCase):
    """The watcher wakes the shared runner; it never runs the global observer sweep."""

    def setUp(self):
        self.watch = load_watch_module()
        self.jobs = Path("/unused/jobs.log")

    def test_resume_duties_request_the_runner_and_never_sweep_observers(self):
        import dispatch_batch_obligations as obligations
        with mock.patch.object(obligations, "ensure_observers",
                               side_effect=AssertionError("global sweep")) as sweep, \
             mock.patch.object(obligations, "request_runner", return_value=True) as request:
            self.watch._resume_batch_duties(self.jobs)
        request.assert_called_once_with(self.jobs)
        sweep.assert_not_called()

    def test_resume_duties_swallow_a_failed_wake(self):
        import dispatch_batch_obligations as obligations
        with mock.patch.object(obligations, "request_runner", side_effect=RuntimeError("boom")):
            self.watch._resume_batch_duties(self.jobs)

    def test_main_wakes_the_runner_once_per_cycle_without_a_sweep(self):
        import dispatch_batch_obligations as obligations
        argv = ["--jobs", str(self.jobs), "--agent-home", "/unused", "--attempt-id", "att-x",
                "--pid", "4242", "--pid-start", "1"]
        with mock.patch.object(obligations, "ensure_observers",
                               side_effect=AssertionError("global sweep")), \
             mock.patch.object(obligations, "request_runner", return_value=False) as request, \
             mock.patch.object(self.watch, "watch", return_value=0), \
             mock.patch.object(self.watch, "attempt_record", return_value=(None, {})), \
             mock.patch("route_parent_close.row_requested", return_value=False):
            self.assertEqual(self.watch.main(argv), 0)
        request.assert_called_once()

    def test_main_keeps_its_exact_arguments_to_start_again_on_the_installed_release(self):
        import dispatch_batch_obligations as obligations
        argv = ["--jobs", str(self.jobs), "--agent-home", "/unused", "--attempt-id", "att-x",
                "--pid", "4242", "--pid-start", "1", "--interval", "0.5"]
        seen = []
        with mock.patch.object(obligations, "request_runner", return_value=False), \
             mock.patch.object(self.watch, "watch",
                               side_effect=lambda args: seen.append(args.release_argv) or 0), \
             mock.patch.object(self.watch, "attempt_record", return_value=(None, {})), \
             mock.patch("route_parent_close.row_requested", return_value=False):
            self.assertEqual(self.watch.main(list(argv)), 0)
        self.assertEqual(seen, [argv])


class AttemptRecordIndexTest(unittest.TestCase):
    """The index lookup answers like the first-match text scan it replaced."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.jobs = Path(self.temp.name) / "jobs.log"
        self.watch = load_watch_module()

    @staticmethod
    def line(attempt, status="open", extra=""):
        return (f"2026-07-19T00:00:00Z\t{status}\t/r\t/w\towner\t"
                f"attempt_schema_version=2,attempt_id={attempt}{extra}")

    def text_scan(self, attempt):
        for text in self.jobs.read_text(encoding="utf-8", errors="replace").splitlines():
            if attempt not in text:
                continue
            fields = text.split("\t")
            if len(fields) == 6:
                meta = self.watch.parse_registry_metadata(fields[5])
                if meta.get("attempt_id") == attempt:
                    return fields[1], meta
        return None, {}

    def check(self, attempt):
        self.assertEqual(self.watch.attempt_record(self.jobs, attempt), self.text_scan(attempt))

    def test_rows_missing_duplicate_tail_and_rewrite_match_the_text_scan(self):
        rows = [self.line(f"att-{i}") for i in range(30)]
        # A duplicate attempt (an abnormal file shape): the first row wins.
        rows += [self.line("att-dup", "open", ",marker=first"), self.line("att-dup", "done", ",marker=second")]
        self.jobs.write_text("\n".join(rows) + "\n", encoding="utf-8")
        for attempt in ("att-0", "att-29", "att-dup", "att-absent"):
            self.check(attempt)
        self.assertEqual(self.watch.attempt_record(self.jobs, "att-dup")[0], "open")
        # A row only in the partial (unterminated) tail.
        with self.jobs.open("a", encoding="utf-8") as handle:
            handle.write(self.line("att-tail", "running"))
        self.check("att-tail")
        self.assertEqual(self.watch.attempt_record(self.jobs, "att-tail")[0], "running")
        # An atomic rewrite replacing a row in place shows the new status.
        replacement = self.jobs.with_suffix(".next")
        replacement.write_text("\n".join(
            self.line("att-3", "done", ",note=rewritten") if i == 3 else row for i, row in enumerate(rows)
        ) + "\n", encoding="utf-8")
        replacement.replace(self.jobs)
        self.check("att-3")
        self.assertEqual(self.watch.attempt_record(self.jobs, "att-3")[0], "done")

    def test_unreadable_registry_is_unknown(self):
        self.assertEqual(self.watch.attempt_record(self.jobs, "att-0"), (None, {}))

    def test_noncanonical_separators_use_the_text_path(self):
        self.jobs.write_text(self.line("att-a") + "\x0b\n" + self.line("att-b", "done") + "\n",
                             encoding="utf-8")
        self.check("att-b")


class WatchLifetimeTest(unittest.TestCase):
    """A watcher lives while its owner lives and follows the owner out; nothing else ends it."""

    class Stop(Exception):
        pass

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.jobs = Path(self.temp.name) / "jobs.log"
        self.watch = load_watch_module()
        self.args = SimpleNamespace(jobs=self.jobs, attempt_id="att-life", pid=4242,
                                    pid_start="1", interval=0.01)
        patches = [
            mock.patch("route_parent_close.row_requested", return_value=False),
            mock.patch.object(self.watch, "recover_waiting_continuation", return_value=None),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def write(self, status):
        self.jobs.write_text(
            f"2026-07-19T00:00:00Z\t{status}\t/r\t/w\towner\t"
            "attempt_schema_version=2,dispatch_depth=1,worker_type=owner,attempt_id=att-life\n",
            encoding="utf-8")

    def sleeper(self, rounds):
        count = []

        def sleep(_seconds):
            count.append(1)
            if len(count) >= rounds:
                raise self.Stop()
        return sleep

    def test_closed_row_with_live_owner_waits_then_follows_the_owner_out(self):
        self.write("done")
        owner_alive = [True]

        def sleep(_seconds):
            sleeps.append(1)
            if len(sleeps) == 5:
                owner_alive[0] = False

        sleeps = []
        with mock.patch.object(self.watch, "process_start",
                               side_effect=lambda _pid: "1" if owner_alive[0] else None), \
             mock.patch.object(self.watch.time, "sleep", side_effect=sleep), \
             mock.patch.object(self.watch, "reconcile_orphan_cascade", return_value=0) as cascade:
            self.assertEqual(self.watch.watch(self.args), 0)
        self.assertEqual(len(sleeps), 5, "waited exactly while the owner was alive")
        cascade.assert_called_once_with(self.args)

    def test_open_row_with_live_owner_is_never_ended_by_the_watcher(self):
        self.write("open")
        with mock.patch.object(self.watch, "process_start", return_value="1"), \
             mock.patch.object(self.watch.time, "sleep", side_effect=self.sleeper(50)), \
             mock.patch.object(self.watch, "reconcile_exact_exit") as exited, \
             mock.patch.object(self.watch, "reconcile_orphan_cascade") as cascade:
            with self.assertRaises(self.Stop):
                self.watch.watch(self.args)
        exited.assert_not_called()
        cascade.assert_not_called()

    def test_open_row_with_exited_quiescent_owner_reconciles_the_exit(self):
        self.write("open")
        quiescent = SimpleNamespace(process_state="quiescent")
        with mock.patch.object(self.watch, "process_start", return_value=None), \
             mock.patch.object(self.watch, "observed_owner_lifecycle",
                               return_value=(quiescent, "", {})), \
             mock.patch.object(self.watch, "reconcile_exact_exit", return_value=0) as exited:
            self.assertEqual(self.watch.watch(self.args), 0)
        exited.assert_called_once_with(self.args)

    def test_a_live_owner_keeps_the_watch_on_its_release_through_every_wait(self):
        self.write("open")
        self.args.release_argv = ["--jobs", str(self.jobs), "--attempt-id", "att-life"]
        import peer_obligations
        with mock.patch("parent_next_directive.release_moved", return_value=True), \
             mock.patch.object(self.watch, "process_start", return_value="1"), \
             mock.patch.object(self.watch.time, "sleep", side_effect=self.sleeper(5)), \
             mock.patch.object(peer_obligations, "follow_installed_release") as follow:
            with self.assertRaises(self.Stop):
                self.watch.watch(self.args)
            self.write("killed")
            with self.assertRaises(self.Stop):
                self.watch.watch(self.args)
        follow.assert_not_called()

    def test_only_an_ended_attempt_moves_and_an_unmoved_install_reads_nothing(self):
        self.args.release_argv = ["--jobs", str(self.jobs), "--attempt-id", "att-life"]
        quiescent = SimpleNamespace(process_state="quiescent")
        live = SimpleNamespace(process_state="live")
        import peer_obligations
        for moved, owner, observed, expected in (
                (False, None, quiescent, False),
                (True, "1", quiescent, False),
                (True, None, live, False),
                (True, None, quiescent, True),
                (True, None, None, True)):
            with self.subTest(moved=moved, owner=owner, observed=observed), \
                 mock.patch("parent_next_directive.release_moved", return_value=moved), \
                 mock.patch.object(self.watch, "process_start", return_value=owner), \
                 mock.patch.object(self.watch, "observed_owner_lifecycle",
                                   return_value=(observed, "", {})) as lifecycle, \
                 mock.patch.object(peer_obligations, "follow_installed_release") as follow:
                self.watch.follow_installed_release(self.args)
            if expected:
                follow.assert_called_once_with(self.watch.__file__, self.args.release_argv)
            else:
                follow.assert_not_called()
            if not moved:
                lifecycle.assert_not_called()

    def test_an_exited_owner_is_reconciled_without_moving_in_between(self):
        self.write("open")
        self.args.release_argv = ["--jobs", str(self.jobs), "--attempt-id", "att-life"]
        quiescent = SimpleNamespace(process_state="quiescent")
        import peer_obligations
        with mock.patch.object(self.watch, "process_start", return_value=None), \
             mock.patch.object(self.watch, "observed_owner_lifecycle",
                               return_value=(quiescent, "", {})), \
             mock.patch.object(peer_obligations, "follow_installed_release") as follow, \
             mock.patch.object(self.watch, "reconcile_exact_exit", return_value=0) as exited:
            self.assertEqual(self.watch.watch(self.args), 0)
        follow.assert_not_called()
        exited.assert_called_once_with(self.args)

    def test_a_direct_call_without_its_original_arguments_stays(self):
        import peer_obligations
        with mock.patch.object(peer_obligations, "follow_installed_release") as follow:
            self.watch.follow_installed_release(self.args)
        follow.assert_not_called()

    def test_finish_recovery_reads_only_the_owner_and_its_children(self):
        self.jobs.write_text(
            "".join(f"2026-07-19T00:00:00Z\tdone\t/r\t/w\tx\tattempt_schema_version=2,attempt_id=att-o{i}\n"
                    for i in range(25))
            + "2026-07-19T00:00:00Z\tdone\t/r\t/w\towner\tattempt_schema_version=2,attempt_id=att-life\n"
            + "2026-07-19T00:00:00Z\tdone\t/r\t/w\tchild\t"
              "attempt_schema_version=2,attempt_id=att-kid,parent_attempt_id=att-life\n",
            encoding="utf-8")
        import dispatch_supervision
        seen = {}

        def pending(rows, owned, *_args, **_kwargs):
            seen.update(rows=set(rows), owned=set(owned))
            return False

        with mock.patch.object(dispatch_supervision, "_pending", side_effect=pending), \
             mock.patch.object(self.watch, "_remove_supervisor_state") as removed:
            self.assertTrue(self.watch._finish_recovery(self.args))
        self.assertEqual(seen["rows"], {"att-life", "att-kid"})
        self.assertEqual(seen["owned"], {"att-life", "att-kid"})
        removed.assert_called_once()

    def test_finish_recovery_refuses_a_registry_with_duplicate_attempts(self):
        row_text = "2026-07-19T00:00:00Z\tdone\t/r\t/w\tx\tattempt_schema_version=2,attempt_id=att-dup\n"
        self.jobs.write_text(row_text * 2, encoding="utf-8")
        import dispatch_supervision
        with self.assertRaises(dispatch_supervision.SupervisionError):
            self.watch._finish_recovery(self.args)


class SettlingSupervisorExitNoticeTest(unittest.TestCase):
    """BC-SETTLE-NOTICE-1112: a PASS owner's supervisor exit during its own settlement is
    noticed only past the join's settling ceiling; a real loss is noticed at once."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.jobs = Path(self.temp.name) / "jobs.log"
        self.watch = load_watch_module()
        self.args = SimpleNamespace(jobs=self.jobs, attempt_id="att-life", pid=4242,
                                    pid_start="1", interval=2.0)
        self.now = [1000.0]
        self.sleeps = []

    def clock(self):
        return self.now[0]

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now[0] += seconds

    def write(self, status, **extra):
        meta = {"attempt_schema_version": "2", "dispatch_depth": "1", "worker_type": "owner",
                "attempt_id": "att-life", "workflow_completion": "runtime-v1",
                "failure_class": "pass", "note": "completed-supervisor", **extra}
        self.jobs.write_text(f"2026-11-12T00:00:00Z\t{status}\t/r\t/w\towner\t"
                             + ",".join(f"{k}={v}" for k, v in meta.items()) + "\n", encoding="utf-8")

    def finish(self, process_state, process_reason, *, settled_after=None):
        """Run _finish_recovery; `_pending` stays true until `settled_after` seconds pass."""
        import dispatch_contract as contract
        import dispatch_supervision
        from split_module_fixture import patch_split
        start = self.now[0]

        def pending(*_args, **_kwargs):
            return settled_after is None or self.now[0] - start < settled_after

        notices = []
        with patch_split(contract, "observed_attempt_liveness", return_value=SimpleNamespace(
                    process_state=process_state, process_reason=process_reason)), \
                mock.patch("codex_dispatch_terminal.terminal_envelope_observed", return_value=True), \
                mock.patch.object(dispatch_supervision, "_pending", side_effect=pending), \
                mock.patch.object(dispatch_supervision, "materialize",
                                  side_effect=lambda *a, **k: notices.append((self.now[0] - start, k))), \
                mock.patch.object(self.watch, "_remove_supervisor_state") as removed:
            finished = self.watch._finish_recovery(self.args, clock=self.clock, sleep=self.sleep)
        return finished, [at for at, _kw in notices], removed.call_count

    def test_settlement_that_finishes_inside_the_ceiling_records_no_notice(self):
        for reason in ("post-exit-receipt-incomplete", "attempt-descendant-unverifiable"):
            with self.subTest(reason=reason):
                self.write("done")
                self.assertEqual(self.finish("unverifiable", reason, settled_after=150), (True, [], 1))
        with self.subTest(reason="route-completion-pending"):   # quiescent, completion not delivered yet
            self.assertEqual(self.finish("quiescent", "governed-process-group-drained", settled_after=150),
                             (True, [], 1))

    def test_settlement_that_never_finishes_is_noticed_once_at_the_ceiling(self):
        from dispatch_completion_join import SETTLING_NOTICE_SECONDS
        self.write("done")
        finished, notices, removed = self.finish("unverifiable", "post-exit-receipt-incomplete")
        self.assertFalse(finished)
        self.assertEqual(removed, 0)
        self.assertEqual(len(notices), 1)
        self.assertGreaterEqual(notices[0], SETTLING_NOTICE_SECONDS)
        self.assertLess(notices[0], SETTLING_NOTICE_SECONDS + max(self.sleeps))

    def test_a_real_loss_or_running_process_is_noticed_at_once(self):
        cases = {
            "no-terminal-signal": ("open", {"failure_class": "", "note": ""}, "unverifiable",
                                   "post-exit-receipt-incomplete"),
            "owner-alive-supervisor-gone": ("open", {"failure_class": "", "note": ""}, "live", "process-alive"),
            "failed-owner": ("done", {"failure_class": "fail", "note": "dead-runtime"}, "unverifiable",
                             "post-exit-receipt-incomplete"),
            "pass-with-running-process": ("done", {}, "live", "attempt-descendant-live"),
            "pass-other-unknown": ("done", {}, "unverifiable", "process-boot-unverifiable"),
        }
        for name, (status, extra, state, reason) in cases.items():
            with self.subTest(case=name):
                self.sleeps.clear()
                self.write(status, **extra)
                self.assertEqual(self.finish(state, reason), (False, [0.0], 0))
                self.assertEqual(self.sleeps, [])


if __name__ == "__main__":
    unittest.main()
