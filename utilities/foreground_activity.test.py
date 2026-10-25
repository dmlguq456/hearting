#!/usr/bin/env python3
import importlib.util
import hashlib
import json
import math
import os
import sqlite3
from pathlib import Path
import subprocess
import tempfile
import sys
import time
import unittest
from types import SimpleNamespace
from unittest import mock

P = Path(__file__).with_name("foreground_activity.py")
S = importlib.util.spec_from_file_location("foreground_activity", P)
A = importlib.util.module_from_spec(S)
sys.modules[S.name] = A
S.loader.exec_module(A)

import owner_route_binding as OWNER_BINDING


OWNER_ATTEMPT = "att-owner-activity"


def owner_route(root: Path, route_id: str, generation: int, source=None):
    route = {
        "route_id": route_id,
        "route_hash": f"sha256:{route_id}",
        "advance_generation": generation,
        "route_family_key": "family-owner-activity",
        "owner_attempt_id": OWNER_ATTEMPT,
        "cwd": str(root),
        "repo": str(root),
        "capability": "autopilot-code",
        "capability_mode": "debug",
        "effective_intensity": "standard",
        "artifact_root": str(root / "artifacts"),
        "reused_nodes": [],
    }
    if source is not None:
        edge = {"from_route_id": source["route_id"],
                "from_route_hash": source["route_hash"]}
        route.update({"source_route_id": source["route_id"],
                      "source_route_hash": source["route_hash"],
                      "source_route_supersession": edge,
                      "supersession_edges": [edge]})
    path = root / f"{route_id}.json"
    path.write_text(json.dumps(route), encoding="utf-8")
    return path, route


def owner_registry(root: Path, route_path: Path, route: dict, *, status="running",
                   extra_metadata=(), child_rows=()):
    jobs = root / "jobs.log"
    metadata = {
        "attempt_id": OWNER_ATTEMPT,
        "worker_type": "owner",
        "unit": "_kernel/owner",
        "attempt_schema_version": "2",
        "dispatch_depth": "1",
        "registered_worker": "1",
        "execution_surface": "registered-headless",
        "owner_harness": "codex",
        "capability": "autopilot-code",
        "capability_mode": "debug",
        "intensity": "standard",
        "artifact_root": str(root / "artifacts"),
        "parent_sid": "launch-parent-session",
        "owner_route_file": str(route_path),
        "owner_route_id": route["route_id"],
        "owner_route_hash": route["route_hash"],
        "log_file": str(root / "owner.log"),
    }
    metadata.update(dict(extra_metadata))
    jobs.write_text("\t".join(("2026-10-25T00:00:00Z", status, str(root),
                                str(root), "owner", ",".join(
                                    f"{key}={value}" for key, value in metadata.items())))
                    + "\n" + "".join(child_rows), encoding="utf-8")
    (root / "owner.log").write_text("owner supervision\n", encoding="utf-8")
    return jobs, metadata


def adopted_child_row(root: Path, route_path: Path, route: dict) -> str:
    metadata = {
        "attempt_id": "att-owner-activity-child",
        "parent_attempt_id": OWNER_ATTEMPT,
        "attempt_schema_version": "2",
        "dispatch_depth": "2",
        "registered_worker": "1",
        "execution_surface": "registered-headless",
        "worker_type": "stage",
        "unit": "dev/backend",
        "route_file": str(route_path),
        "route_id": route["route_id"],
        "route_hash": route["route_hash"],
        "route_node": "execute",
        "capability": route["capability"],
        "capability_mode": route["capability_mode"],
        "artifact_root": route["artifact_root"],
        "launch_started": "1",
    }
    return "\t".join(("2026-10-25T00:00:01Z", "open", str(root), str(root),
                        "child", ",".join(f"{key}={value}" for key, value in metadata.items()))) + "\n"


def owner_args(root: Path, jobs: Path, route_path: Path, route: dict):
    return SimpleNamespace(parent_attempt_id=OWNER_ATTEMPT, jobs=str(jobs),
                           route_file=str(route_path), route_id=route["route_id"],
                           route_hash=route["route_hash"], worktree=str(root))


class ForegroundActivityTests(unittest.TestCase):
    def test_owner_is_exempt_before_defaults_are_read(self):
        with mock.patch.object(A.importlib.util, "spec_from_file_location", side_effect=AssertionError):
            policy = A.resolve_activity_policy("owner")
        self.assertIsNone(policy.window_seconds)
        self.assertEqual(policy.source, "owner-exempt")

    def test_policy_reads_shipped_defaults_and_context_is_optional_at_zero(self):
        policy = A.resolve_activity_policy("stage")
        self.assertEqual(policy.window_seconds, 1800)
        disabled = A.ActivityPolicy(None, "dispatch-defaults")
        self.assertIsNone(A.make_activity_context(
            disabled, worker_type="stage", harness="codex", log_path=Path("/tmp/a"),
            jobs=Path("/tmp/jobs.log"), metadata={}, runtime_executable="codex",
        ))

    def test_proc_stat_parser_uses_last_closing_parenthesis(self):
        from foreground_activity import _stat
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "stat"
            tail = ["S", "12", "10"] + ["0"] * 8 + ["2", "3", "4", "5"] + ["0"] * 4 + ["987"]
            p.write_text("42 (name ) with spaces) " + " ".join(tail) + "\n", encoding="utf-8")
            sample = _stat(p)
        self.assertEqual(sample["ppid"], 12)
        self.assertEqual(sample["pgid"], 10)
        self.assertEqual(sample["self_cpu"], 5)
        self.assertEqual(sample["children_cpu"], 9)
        self.assertEqual(sample["start"], "987")

    def test_context_helper_skips_owner_and_disabled_policy_inputs(self):
        arguments = dict(
            worker_type="owner", harness="codex", log_path=Path("/no/log"),
            jobs=Path("/no/jobs"), launch_metadata={}, attempt_id="a",
            route_id=None, route_node=None, environment={}, worktree=Path("/work"),
        )
        with mock.patch.object(A, "resolve_activity_policy", side_effect=AssertionError):
            self.assertIsNone(A.build_activity_context(**arguments))

        arguments["worker_type"] = "stage"
        with mock.patch.object(A, "resolve_activity_policy", return_value=A.ActivityPolicy(None, "disabled")):
            self.assertIsNone(A.build_activity_context(**arguments))

    def test_context_helper_builds_for_each_non_owner_worker_type(self):
        for worker_type in ("stage", "review", "support", "frame"):
            context = A.build_activity_context(
                worker_type=worker_type, harness="codex", log_path=Path("/tmp/log"),
                jobs=Path("/tmp/jobs"), launch_metadata={}, attempt_id="a",
                route_id="route", route_node="execute", environment={"CODEX_HOME": "/tmp/codex"},
                worktree=Path("/tmp/work"),
            )
            self.assertIsNotNone(context, worker_type)
            self.assertEqual(context.harness, "codex")
            self.assertEqual(context.transcript_roots, (Path("/tmp/codex/sessions"),))

    def test_context_failure_warns_once_and_returns_none(self):
        from contextlib import redirect_stderr
        from io import StringIO
        error = StringIO()
        with redirect_stderr(error):
            context = A.build_activity_context(
                worker_type="stage", harness="codex", log_path=Path("/tmp/log"),
                jobs=Path("/tmp/jobs"), launch_metadata={}, attempt_id="a",
                route_id=None, route_node=None, environment={}, worktree=Path("/tmp/work"),
            )
        self.assertIsNone(context)
        self.assertEqual(len(error.getvalue().splitlines()), 1)
        self.assertTrue(error.getvalue().startswith("warning="))

    def opencode_context(self, root, *, launch_metadata=None, environment=None):
        return A.build_activity_context(
            worker_type="stage", harness="opencode", log_path=root / "attempt.jsonl",
            jobs=root / "jobs.log", launch_metadata=launch_metadata or {}, attempt_id="a",
            route_id="route", route_node="test", environment=environment or {"XDG_DATA_HOME": str(root)},
            worktree=root,
        )

    def test_optional_opencode_runtime_keeps_context_without_warning(self):
        from contextlib import redirect_stderr
        from io import StringIO
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            error = StringIO()
            with redirect_stderr(error):
                shared = self.opencode_context(root)
            self.assertEqual(error.getvalue(), "")
            self.assertEqual(shared.opencode_db, root / "opencode/opencode.db")
            self.assertEqual(shared.transcript_roots, ())
            self.assertEqual(shared.metadata["attempt_id"], "a")
            self.assertIsNone(A.ForegroundActivityMonitor(10, "1", shared)._opencode_session_state())
            private = self.opencode_context(root, launch_metadata={"opencode_runtime_dir": str(root / "private")})
            self.assertEqual(private.transcript_roots, (
                root / "private/data/opencode/opencode.db", root / "private/data/opencode/opencode.db-wal"))
            self.assertIsNone(private.opencode_db)
            overridden = self.opencode_context(root, environment={
                "OPENCODE_DATA_DIR": str(root / "shared"), "OPENCODE_DB": "custom.db"})
            self.assertEqual(overridden.opencode_db, root / "shared/custom.db")

    def test_shared_database_activity_is_exact_session_only(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            context = self.opencode_context(root)
            context.log_path.write_text(json.dumps({"type": "step_start", "sessionID": "ses_own"}) + "\n")
            context.opencode_db.parent.mkdir()
            db = sqlite3.connect(context.opencode_db)
            self.addCleanup(db.close)
            db.executescript("""
                CREATE TABLE session_v2 (id TEXT PRIMARY KEY, directory TEXT, time_updated INTEGER);
                CREATE TABLE session_message (session_id TEXT, type TEXT, seq INTEGER,
                    time_created INTEGER, time_updated INTEGER, data TEXT);
            """)
            for sid in ("ses_own", "ses_other"):
                db.execute("INSERT INTO session_v2 VALUES (?, ?, 1)", (sid, str(root)))
                db.execute("INSERT INTO session_message VALUES (?, 'assistant', 1, 1, 1, ?)",
                           (sid, json.dumps({"content": [{"type": "text", "text": "initial"}]})))
            db.commit()
            monitor = A.ForegroundActivityMonitor(10, "1", context)
            table = {(10, "1"): {"self_cpu": 0, "children_cpu": 0}}
            with mock.patch.object(monitor, "_processes", return_value=table), \
                 mock.patch.object(monitor, "_members", return_value=set(table)), \
                 mock.patch.object(monitor, "_runtime_lineage", return_value=set(table)), \
                 mock.patch.object(monitor, "_lease_active", return_value=False):
                # The consumer renews the no-progress clock on active or
                # uncertain, so an observed session must be neither.
                baseline = monitor.sample()
                self.assertEqual((baseline.active, baseline.uncertain), (False, False))
                db.execute("UPDATE session_message SET time_updated=2 WHERE session_id='ses_other'")
                db.commit()
                other = monitor.sample()
                self.assertEqual((other.active, other.uncertain), (False, False),
                                 "another session reset the attempt clock")
                db.execute("UPDATE session_v2 SET time_updated=2 WHERE id='ses_own'")
                db.commit()
                metadata = monitor.sample()
                self.assertEqual((metadata.active, metadata.uncertain), (False, False),
                                 "session metadata alone reset the attempt clock")
                db.execute("UPDATE session_message SET time_updated=2 WHERE session_id='ses_own'")
                db.commit()
                own = monitor.sample()
                self.assertEqual((own.active, own.uncertain), (True, False))
                self.assertIn("exact-opencode-session-change", own.reasons)
                db.close()
                context.opencode_db.unlink()
                missing = monitor.sample()
                self.assertFalse(missing.active, "lost access was treated as progress")
                self.assertTrue(missing.uncertain, "lost access could incorrectly expire the no-progress window")
                context.log_path.write_text(context.log_path.read_text() + "attempt-owned progress\n")
                self.assertIn("attempt-file-change", monitor.sample().reasons)

    def test_owner_observation_ignores_supervisor_logs_transcript_roots_and_aggregate_child_cpu(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            log = root / "attempt.jsonl"
            broad = root / "other-session.jsonl"
            exact = root / "exact-session.jsonl"
            log.write_text("supervisor bookkeeping 1\n")
            broad.write_text("other session 1\n")
            exact.write_text("payload 1\n")
            context = A.ForegroundActivityContext(
                window_seconds=7200, harness="claude", log_path=log,
                metadata={"attempt_id": "owner-1"},
                transcript_roots=(root,), exact_files=(exact,), owner_turn=True,
            )
            monitor = A.ForegroundActivityMonitor(10, "1", context)
            table = {
                (10, "1"): {"self_cpu": 1, "children_cpu": 10},
                (11, "2"): {"self_cpu": 3, "children_cpu": 100},
            }
            members = set(table)
            with mock.patch.object(monitor, "_processes", return_value=table), \
                 mock.patch.object(monitor, "_members", return_value=members), \
                 mock.patch.object(monitor, "_runtime_lineage", return_value={(10, "1")}), \
                 mock.patch.object(monitor, "_lease_active", return_value=False), \
                 mock.patch.object(monitor, "_opencode_session_state", return_value=None):
                baseline = monitor.sample()
                self.assertEqual((baseline.active, baseline.uncertain), (False, False))
                table[(10, "1")] = {"self_cpu": 1, "children_cpu": 99}
                log.write_text("supervisor bookkeeping 2\n")
                broad.write_text("other session 2\n")
                quiet = monitor.sample()
                self.assertEqual((quiet.active, quiet.uncertain), (False, False))
                exact.write_text("payload 2\n")
                changed = monitor.sample()
                self.assertTrue(changed.active)
                self.assertIn("attempt-file-change", changed.reasons)

    def test_owner_process_members_exclude_process_group_siblings_and_attempt_tags(self):
        context = A.ForegroundActivityContext(
            window_seconds=7200, harness="codex", log_path=Path("/attempt.log"),
            metadata={"attempt_id": "owner-1"}, owner_turn=True,
        )
        monitor = A.ForegroundActivityMonitor(10, "1", context)
        table = {
            (10, "1"): {"ppid": 1, "pgid": 10},
            (11, "2"): {"ppid": 1, "pgid": 10},  # same group, not a descendant
            (12, "3"): {"ppid": 10, "pgid": 12},  # exact runtime descendant
        }
        with mock.patch("dispatch_contract.attempt_tagged_descendants",
                        side_effect=AssertionError("owner must not broaden by tag")):
            self.assertEqual(monitor._members(table), {(10, "1"), (12, "3")})

    def test_owner_pid_reuse_is_uncertain_and_never_becomes_activity(self):
        context = A.ForegroundActivityContext(
            window_seconds=7200, harness="codex", log_path=Path("/attempt.log"),
            metadata={"attempt_id": "owner-1"}, owner_turn=True,
        )
        monitor = A.ForegroundActivityMonitor(10, "birth-1", context)
        table = {(10, "birth-2"): {"ppid": 1, "pgid": 10}}
        with self.assertRaisesRegex(RuntimeError, "identity changed or disappeared"):
            monitor._members(table)

    def test_owner_lease_requires_exact_process_route_and_live_deadline(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            jobs = root / "jobs.log"
            jobs.touch()
            process = subprocess.Popen(
                [sys.executable, "-c", "import time; print('ready', flush=True); time.sleep(30)"],
                start_new_session=True,
                stdout=subprocess.PIPE, text=True,
            )
            try:
                # /proc/cmdline can be empty during exec; wait for this fixture's
                # interpreter before hashing its command identity.
                self.assertEqual(process.stdout.readline(), "ready\n")
                proc = Path("/proc") / str(process.pid)
                stat = A._stat(proc / "stat")
                argv = (proc / "cmdline").read_bytes().rstrip(b"\0").split(b"\0")
                record = {
                    "attempt_id": "owner-1", "schema_version": 1,
                    "route_id": "route-1", "route_node": "_owner",
                    "pid": process.pid, "pid_start": stat["start"],
                    "pgid": process.pid, "deadline": time.time() + 30,
                    "command_digest": hashlib.sha256(b"\0".join(argv)).hexdigest(),
                }
                lease = root / "verification-leases" / "owner-1.json"
                lease.parent.mkdir()
                lease.write_text(json.dumps(record), encoding="utf-8")
                context = A.ForegroundActivityContext(
                    window_seconds=7200, harness="codex", log_path=root / "attempt.log",
                    metadata={"attempt_id": "owner-1", "route_id": "route-1"},
                    jobs=jobs, owner_turn=True,
                )
                monitor = A.ForegroundActivityMonitor(process.pid, stat["start"], context)
                self.assertTrue(monitor._lease_active())
                lease.write_text(json.dumps({**record, "pid_start": "reused"}), encoding="utf-8")
                self.assertFalse(monitor._lease_active())
                lease.write_text(json.dumps({**record, "route_id": "route-other"}), encoding="utf-8")
                self.assertFalse(monitor._lease_active())
                lease.write_text(json.dumps({**record, "deadline": time.time() - 1}), encoding="utf-8")
                self.assertFalse(monitor._lease_active())
            finally:
                process.terminate()
                process.wait(timeout=5)
                process.stdout.close()

    def test_owner_turn_factory_resolves_the_real_launch_binding_without_a_route_node(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            route_path, route = owner_route(root, "rt-owner-launch", 0)
            jobs, _metadata = owner_registry(root, route_path, route)
            args = owner_args(root, jobs, route_path, route)
            process = SimpleNamespace(pid=4242)
            monitor = mock.Mock()
            with mock.patch.object(OWNER_BINDING.ROUTE, "verify_route",
                                   side_effect=lambda raw, **_kwargs: raw), \
                 mock.patch.object(A, "_stat", return_value={"start": "77"}), \
                 mock.patch.object(A, "ForegroundActivityMonitor", return_value=monitor) as create:
                activity = A.owner_turn_activity(
                    args, process, harness="codex", window_seconds=20)
            context = create.call_args.args[2]
            self.assertTrue(context.owner_turn)
            self.assertEqual(context.metadata["attempt_id"], OWNER_ATTEMPT)
            self.assertEqual(context.metadata["route_id"], route["route_id"])
            self.assertEqual(context.metadata["route_hash"], route["route_hash"])
            self.assertEqual(context.metadata["runtime_pid"], "4242")
            self.assertEqual(context.metadata["runtime_start"], "77")
            self.assertNotIn("route_node", context.metadata)
            activity.expired(now=0)
            monitor.sample.assert_called_once()

    def test_owner_turn_factory_follows_an_adopted_continuation_binding(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            first_path, first = owner_route(root, "rt-owner-r0", 0)
            next_path, next_route = owner_route(root, "rt-owner-r1", 1, source=first)
            jobs, _metadata = owner_registry(root, first_path, first)
            source = OWNER_BINDING.OwnerRouteBinding(
                str(first_path.resolve()), first["route_id"], first["route_hash"])
            target = OWNER_BINDING.OwnerRouteBinding(
                str(next_path.resolve()), next_route["route_id"], next_route["route_hash"])
            with mock.patch.object(OWNER_BINDING.ROUTE, "verify_route",
                                   side_effect=lambda raw, **_kwargs: raw):
                OWNER_BINDING.publish_owner_route_advance(
                    jobs, owner_attempt_id=OWNER_ATTEMPT, source=source, target=target,
                    from_generation=0, to_generation=1,
                    route_family_key="family-owner-activity")
            with jobs.open("a", encoding="utf-8") as stream:
                stream.write(adopted_child_row(root, next_path, next_route))
            args = owner_args(root, jobs, next_path, next_route)
            monitor = mock.Mock()
            with mock.patch.object(OWNER_BINDING.ROUTE, "verify_route",
                                   side_effect=lambda raw, **_kwargs: raw), \
                 mock.patch.object(A, "_stat", return_value={"start": "78"}), \
                 mock.patch.object(A, "ForegroundActivityMonitor", return_value=monitor) as create:
                activity = A.owner_turn_activity(
                    args, SimpleNamespace(pid=4243), harness="codex", window_seconds=20)
            context = create.call_args.args[2]
            self.assertEqual(context.metadata["route_id"], next_route["route_id"])
            self.assertEqual(context.metadata["route_hash"], next_route["route_hash"])
            self.assertNotIn("route_node", context.metadata)
            activity.expired(now=0)
            monitor.sample.assert_called_once()

    def test_owner_turn_factory_keeps_conflicting_or_invalid_identity_uncertain(self):
        cases = ("stage-shaped-row", "refreshed-args-mismatch", "sealed-hash-conflict")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                route_path, route = owner_route(root, "rt-owner-conflict", 0)
                extra = {"route_id": "rt-other", "route_node": "fabricated"} \
                    if case == "stage-shaped-row" else {}
                if case == "sealed-hash-conflict":
                    extra = {"owner_route_hash": "sha256:conflict"}
                jobs, _metadata = owner_registry(
                    root, route_path, route, extra_metadata=extra)
                args = owner_args(root, jobs, route_path, route)
                if case == "refreshed-args-mismatch":
                    args.route_id = "rt-other"
                with mock.patch.object(OWNER_BINDING.ROUTE, "verify_route",
                                       side_effect=lambda raw, **_kwargs: raw), \
                     mock.patch.object(A, "_stat", return_value={"start": "79"}), \
                     mock.patch.object(A, "ForegroundActivityMonitor",
                                       side_effect=AssertionError("conflicting binding sampled")):
                    uncertain = A.owner_turn_activity(
                        args, SimpleNamespace(pid=4244), harness="codex", window_seconds=20)
                self.assertFalse(uncertain.expired(now=0))
                self.assertTrue(uncertain.observer().uncertain)

    def test_confirmed_quiet_expires_after_factory_binds_real_owner_row(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            route_path, route = owner_route(root, "rt-owner-quiet", 0)
            jobs, _metadata = owner_registry(root, route_path, route)
            args = owner_args(root, jobs, route_path, route)
            process = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(10)"],
                start_new_session=True,
            )
            try:
                with mock.patch.object(OWNER_BINDING.ROUTE, "verify_route",
                                       side_effect=lambda raw, **_kwargs: raw), \
                     mock.patch.object(A.ForegroundActivityMonitor, "_runtime_lineage",
                                       return_value={(process.pid, A._stat(
                                           Path("/proc") / str(process.pid) / "stat")["start"])}):
                    activity = A.owner_turn_activity(
                        args, process, harness="codex", window_seconds=0.1)
                    self.assertFalse(activity.expired(now=1.0))
                    self.assertTrue(activity.expired(now=1.11))
            finally:
                process.terminate()
                process.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
