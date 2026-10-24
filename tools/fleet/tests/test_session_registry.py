#!/usr/bin/env python3
"""Cross-harness session registry (F-26b, plan.md §7.1/§7.4) — B-8.

Covers: session_registry.py reader/writer contract, its claude.py/codex.py/opencode.py
consumers (B-3/B-4/B-5), the managed Codex launch lifecycle that writes it
(B-2a/B-2b/B-2c), and the four display-bug fixes (B-6).
"""
from __future__ import annotations

import importlib.util
import json
import os
import stat
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_TOOLS_DIR = os.path.dirname(os.path.dirname(_TESTS_DIR))
_REPO_ROOT = os.path.dirname(_TOOLS_DIR)
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)

from fleet import model, session_registry                        # noqa: E402
from fleet.collectors import claude as claude_collector           # noqa: E402
from fleet.collectors import codex as codex_collector             # noqa: E402
from fleet.collectors import liveness                             # noqa: E402
from fleet.model import Session                                   # noqa: E402


def _load_hyphenated(name, relative_path):
    """Import a hyphenated-filename module (can't `import` it normally)."""
    path = os.path.join(_REPO_ROOT, relative_path)
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _RegistryDirMixin:
    """Points FLEET_SESSION_REGISTRY_DIR at a private tmpdir for the test's duration."""

    def setUp(self):
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._old_env = os.environ.get("FLEET_SESSION_REGISTRY_DIR")
        os.environ["FLEET_SESSION_REGISTRY_DIR"] = self._tmp.name
        self.addCleanup(self._restore_env)

    def _restore_env(self):
        if self._old_env is None:
            os.environ.pop("FLEET_SESSION_REGISTRY_DIR", None)
        else:
            os.environ["FLEET_SESSION_REGISTRY_DIR"] = self._old_env


class ConstantsTest(unittest.TestCase):
    def test_frozen_shapes(self):
        self.assertEqual(session_registry.HARNESSES, ("claude", "codex", "opencode"))
        self.assertEqual(len(session_registry.FIELDS), 16)
        self.assertEqual(session_registry.STATUSES, ("idle", "busy", "shell", "exited"))
        self.assertEqual(
            session_registry.WRITER_SUPPORT,
            {"claude": "runtime-native", "codex": "hearting-managed",
             "opencode": "not-implemented"},
        )

    def test_writer_support_rejects_unknown_harness(self):
        with self.assertRaises(ValueError):
            session_registry.writer_support("gpt-cli")


class ReadWriteRoundTripTest(_RegistryDirMixin, unittest.TestCase):
    def test_round_trip(self):
        session_registry.write("codex", 4242, {"cwd": "/repo", "status": "busy"})
        record = session_registry.read("codex", 4242)
        self.assertEqual(record["cwd"], "/repo")
        self.assertEqual(record["status"], "busy")
        self.assertEqual(record["pid"], 4242)
        self.assertEqual(record["harness"], "codex")
        self.assertEqual(set(record), set(session_registry.FIELDS))

    def test_merge_update_preserves_existing_fields(self):
        session_registry.write("codex", 10, {"cwd": "/repo", "kind": "codex-tui"})
        session_registry.write("codex", 10, {"status": "idle"})
        record = session_registry.read("codex", 10)
        self.assertEqual(record["cwd"], "/repo")
        self.assertEqual(record["kind"], "codex-tui")
        self.assertEqual(record["status"], "idle")

    def test_remove_then_read_is_none(self):
        session_registry.write("codex", 11, {"cwd": "/repo"})
        self.assertTrue(session_registry.remove("codex", 11))
        self.assertIsNone(session_registry.read("codex", 11))

    def test_remove_missing_is_true(self):
        self.assertTrue(session_registry.remove("codex", 999999))

    def test_symlink_is_rejected(self):
        real = os.path.join(self._tmp.name, "elsewhere.json")
        with open(real, "w", encoding="utf-8") as handle:
            json.dump({"pid": 12, "cwd": "/x"}, handle)
        target_dir = os.path.join(self._tmp.name, "codex")
        os.makedirs(target_dir, exist_ok=True)
        os.symlink(real, os.path.join(target_dir, "12.json"))
        self.assertIsNone(session_registry.read("codex", 12))

    def test_foreign_uid_is_rejected(self):
        session_registry.write("codex", 13, {"cwd": "/repo"})
        with mock.patch.object(session_registry.os, "getuid", return_value=os.getuid() + 1):
            self.assertIsNone(session_registry.read("codex", 13))

    def test_oversized_file_is_rejected(self):
        directory = os.path.join(self._tmp.name, "codex")
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, "14.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"pid": 14, "cwd": "x" * (session_registry._MAX_BYTES + 1)}, handle)
        self.assertIsNone(session_registry.read("codex", 14))

    def test_corrupt_json_is_none(self):
        directory = os.path.join(self._tmp.name, "codex")
        os.makedirs(directory, exist_ok=True)
        with open(os.path.join(directory, "15.json"), "w", encoding="utf-8") as handle:
            handle.write("{not json")
        self.assertIsNone(session_registry.read("codex", 15))

    def test_write_unsupported_for_runtime_native_and_not_implemented(self):
        with self.assertRaises(session_registry.RegistryWriteUnsupported):
            session_registry.write("claude", 1, {"cwd": "/x"})
        with self.assertRaises(session_registry.RegistryWriteUnsupported):
            session_registry.write("opencode", 1, {"cwd": "/x"})
        with self.assertRaises(session_registry.RegistryWriteUnsupported):
            session_registry.remove("claude", 1)

    def test_write_rejects_unknown_field(self):
        with self.assertRaises(ValueError):
            session_registry.write("codex", 16, {"not_a_real_field": 1})


class ApplyToSessionTest(unittest.TestCase):
    RECORD = {field: None for field in session_registry.FIELDS}

    def _record(self, **over):
        rec = dict(self.RECORD)
        rec.update(over)
        return rec

    def test_claude_and_codex_records_fill_the_same_session_fields(self):
        record = self._record(
            sessionId="sid-1", status="busy", name="my-session", nameSource="user",
            kind="interactive", procStart="123456", startedAt=1000000, updatedAt=1000500,
        )
        claude_sess = Session(harness="claude", pid=1)
        codex_sess = Session(harness="codex", pid=2)
        session_registry.apply_to_session(claude_sess, record, "claude")
        session_registry.apply_to_session(codex_sess, record, "codex")
        for field in ("session_id", "status", "slug", "registry_name", "runtime_name",
                      "kind", "registry_proc_start", "started_at", "updated_at"):
            self.assertEqual(
                getattr(claude_sess, field), getattr(codex_sess, field), field)
        self.assertEqual(claude_sess.session_id, "sid-1")
        self.assertEqual(claude_sess.status, "busy")

    def test_derived_tag_branch_is_claude_only(self):
        record = self._record(name="repo-a9", nameSource="derived")
        claude_sess = Session(harness="claude", pid=1)
        codex_sess = Session(harness="codex", pid=2)
        session_registry.apply_to_session(claude_sess, record, "claude")
        session_registry.apply_to_session(codex_sess, record, "codex")
        self.assertIsNotNone(claude_sess.session_tag)
        self.assertIsNone(codex_sess.session_tag)

    def test_absent_keys_are_none_not_synthesized(self):
        sess = Session(harness="codex", pid=3)
        session_registry.apply_to_session(sess, self._record(), "codex")
        self.assertIsNone(sess.status)
        self.assertIsNone(sess.started_at)
        self.assertIsNone(sess.updated_at)


class ClassifySessionTierOneTest(unittest.TestCase):
    """The registry's `status`/`procStart` reach classify_session harness-neutrally
    (model.py:1262 only special-cases codex when `st is None`)."""

    def setUp(self):
        model.reset_state_tracker()

    def test_status_busy_is_tier1_working(self):
        sess = Session(harness="codex", pid=os.getpid(), cwd="/repo")
        session_registry.apply_to_session(
            sess, {**ApplyToSessionTest.RECORD, "status": "busy"}, "codex")
        state = liveness.classify(sess, time.time())
        self.assertEqual(state, "working")
        self.assertEqual(sess.state_evidence["tier"], 1)

    def test_proc_start_mismatch_is_dead(self):
        sess = Session(harness="codex", pid=os.getpid(), cwd="/repo")
        session_registry.apply_to_session(
            sess,
            {**ApplyToSessionTest.RECORD, "status": "busy", "procStart": "not-the-real-one"},
            "codex",
        )
        state = liveness.classify(sess, time.time())
        self.assertEqual(state, "dead")


class ImportContractTest(unittest.TestCase):
    def test_importable_with_only_tools_on_sys_path(self):
        import subprocess
        code = (
            "import sys; sys.path.insert(0, %r); "
            "from fleet import session_registry; print(session_registry.state_root())"
        ) % _TOOLS_DIR
        with tempfile.TemporaryDirectory() as outside:
            env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
            result = subprocess.run(
                [sys.executable, "-c", code], cwd=outside, env=env,
                capture_output=True, text=True, timeout=20,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(result.stdout.strip())


class ShareManagedRegistryStateTest(unittest.TestCase):
    def _sess(self, pid, *, app_server, managed_dir="/state/dir", **over):
        sess = Session(harness="codex", pid=pid, app_server=app_server, managed_dir=managed_dir)
        for key, value in over.items():
            setattr(sess, key, value)
        return sess

    def test_copies_session_id_and_status_from_server_to_client(self):
        server = self._sess(1, app_server=True, session_id="sid-x", status="busy",
                            updated_at=100.0)
        client = self._sess(2, app_server=False)
        codex_collector.share_managed_registry_state([server, client])
        self.assertEqual(client.session_id, "sid-x")
        self.assertEqual(client.status, "busy")
        self.assertEqual(client.updated_at, 100.0)

    def test_two_servers_in_one_dir_copies_nothing(self):
        s1 = self._sess(1, app_server=True, session_id="a")
        s2 = self._sess(2, app_server=True, session_id="b")
        client = self._sess(3, app_server=False)
        codex_collector.share_managed_registry_state([s1, s2, client])
        self.assertIsNone(client.session_id)

    def test_two_clients_in_one_dir_copies_nothing(self):
        server = self._sess(1, app_server=True, session_id="a")
        c1 = self._sess(2, app_server=False)
        c2 = self._sess(3, app_server=False)
        codex_collector.share_managed_registry_state([server, c1, c2])
        self.assertIsNone(c1.session_id)
        self.assertIsNone(c2.session_id)

    def test_client_with_existing_value_is_not_overwritten(self):
        server = self._sess(1, app_server=True, session_id="server-sid", status="busy")
        client = self._sess(2, app_server=False, session_id="client-own-sid")
        codex_collector.share_managed_registry_state([server, client])
        self.assertEqual(client.session_id, "client-own-sid")

    def test_copy_direction_is_server_to_client_only(self):
        server = self._sess(1, app_server=True)
        client = self._sess(2, app_server=False, session_id="client-sid", status="idle")
        codex_collector.share_managed_registry_state([server, client])
        self.assertIsNone(server.session_id)
        self.assertIsNone(server.status)


class DurationLabelTest(unittest.TestCase):
    def test_window_minutes_10080_labels_7d(self):
        win = codex_collector._window_from_limit(
            "primary", {"used_percent": 91, "window_minutes": 10080}, "5h")
        self.assertEqual(win["label"], "7d")

    def test_window_minutes_zero_or_missing_is_no_label_source(self):
        self.assertIsNone(codex_collector._duration_label_from_minutes(0))
        self.assertIsNone(codex_collector._duration_label_from_minutes(None))
        self.assertIsNone(codex_collector._duration_label_from_minutes(True))


class ManagedClientVisibilityTest(unittest.TestCase):
    def test_unpaired_app_server_with_session_id_is_visible(self):
        sess = Session(harness="codex", pid=1, app_server=True, session_id="sid-x",
                       liveness="idle")
        sess._managed_client_present = False
        self.assertTrue(model.session_parent_visible(sess))

    def test_paired_app_server_stays_hidden(self):
        sess = Session(harness="codex", pid=1, app_server=True, session_id="sid-x",
                       liveness="idle")
        sess._managed_client_present = True
        self.assertFalse(model.session_parent_visible(sess))

    def test_app_server_without_session_id_stays_hidden(self):
        sess = Session(harness="codex", pid=1, app_server=True, session_id=None,
                       liveness="idle")
        sess._managed_client_present = False
        self.assertFalse(model.session_parent_visible(sess))


class ManagedEntryLifecycleTest(_RegistryDirMixin, unittest.TestCase):
    """B-2a: import codex-managed-entry.py, mock Popen/run/wait_socket/terminate."""

    def setUp(self):
        super().setUp()
        self.entry = _load_hyphenated(
            "codex_managed_entry_under_test", "utilities/codex-managed-entry.py")
        self.workdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.workdir.cleanup)
        base = Path(self.workdir.name)
        self.home = base / "codex-home"
        self.state = base / "state"
        self.workspace = base / "workspace"
        for path in (self.home, self.state, self.workspace):
            path.mkdir()
            os.chmod(path, 0o700)
        (self.home / "auth.json").write_text("{}\n", encoding="utf-8")
        os.chmod(self.home / "auth.json", 0o600)

    def _args(self):
        return self.entry.parser().parse_args([
            "--codex", "fake-codex",
            "--codex-home", str(self.home),
            "--state-dir", str(self.state),
            "--workspace", str(self.workspace),
        ])

    @staticmethod
    def _fake_run(cmd, **_kwargs):
        import subprocess
        if "--help" in cmd:
            marker = "--listen" if "app-server" in cmd else "--remote"
            return subprocess.CompletedProcess(cmd, 0, stdout=marker, stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    class _FakeProc:
        def __init__(self, pid, returncode=0, on_wait=None):
            self.pid = pid
            self.returncode = returncode
            self._on_wait = on_wait
            self._waited = False

        def poll(self):
            # Like Popen: running until wait() has reaped it.
            return self.returncode if self._waited else None

        def wait(self, timeout=None):
            if self._on_wait is not None:
                self._on_wait()
            self._waited = True
            return self.returncode

    def _run(self, *, fail_stage=None, on_client_wait=None):
        """Execute entry.execute() with Popen mocked to hand out fixed pids
        (app-server=90001, gateway=90002, client=90003) in call order. `finally`
        removes both registry records before execute() returns, so a test that
        wants to see them while still present passes `on_client_wait` — invoked
        from the client's `.wait()`, the last mocked call before cleanup."""
        calls = []

        def fake_popen(cmd, **kwargs):
            calls.append((list(cmd), kwargs))
            index = len(calls)
            if fail_stage == index:
                raise OSError("simulated launch failure")
            if index == 1:
                return self._FakeProc(90001)
            if index == 2:
                return self._FakeProc(90002)
            return self._FakeProc(90003, on_wait=on_client_wait)

        with mock.patch.object(self.entry, "wait_socket", return_value=None), \
             mock.patch.object(self.entry, "terminate", return_value=None), \
             mock.patch.object(self.entry.subprocess, "run", side_effect=self._fake_run), \
             mock.patch.object(self.entry.subprocess, "Popen", side_effect=fake_popen):
            args = self._args()
            returncode = self.entry.execute(args)
        return returncode, calls

    def test_writes_both_records_with_their_kinds(self):
        snapshot = {}

        def capture():
            snapshot["app_server"] = session_registry.read("codex", 90001)
            snapshot["client"] = session_registry.read("codex", 90003)

        self._run(on_client_wait=capture)
        self.assertEqual(snapshot["app_server"]["kind"], "codex-app-server")
        self.assertEqual(snapshot["client"]["kind"], "codex-tui")
        self.assertEqual(snapshot["app_server"]["entrypoint"], "codex-managed-entry")
        self.assertEqual(snapshot["client"]["entrypoint"], "codex-managed-entry")

    def test_gateway_receives_only_the_app_server_pid(self):
        _, calls = self._run()
        gateway_cmd = calls[1][0]
        self.assertIn("--session-registry-pid", gateway_cmd)
        idx = gateway_cmd.index("--session-registry-pid")
        self.assertEqual(gateway_cmd[idx + 1], "90001")
        self.assertNotIn("90003", gateway_cmd)

    def test_client_is_launched_with_popen_in_the_same_session(self):
        returncode, calls = self._run()
        _client_cmd, client_kwargs = calls[2]
        self.assertNotIn("start_new_session", client_kwargs)
        self.assertNotIn("stdin", client_kwargs)
        self.assertNotIn("stdout", client_kwargs)
        self.assertNotIn("stderr", client_kwargs)
        self.assertEqual(returncode, 0)

    def test_finally_removes_both_records(self):
        self._run()
        self.assertIsNone(session_registry.read("codex", 90001))
        self.assertIsNone(session_registry.read("codex", 90003))

    def test_client_launch_failure_removes_only_the_app_server_record(self):
        with self.assertRaises(OSError):
            self._run(fail_stage=3)
        self.assertIsNone(session_registry.read("codex", 90001))
        self.assertIsNone(session_registry.read("codex", 90003))

    def test_app_server_launch_failure_removes_nothing(self):
        with self.assertRaises(OSError):
            self._run(fail_stage=1)
        self.assertIsNone(session_registry.read("codex", 90001))


class ManagedGatewayRegistryTest(unittest.TestCase):
    """B-2b: pending-field merge/flush lifecycle, directly against ManagedGateway."""

    def setUp(self):
        self.gateway_module = _load_hyphenated(
            "codex_managed_gateway_under_test", "utilities/codex-managed-gateway.py")

    def _gateway(self, session_registry_pid=90001):
        base = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(base, ignore_errors=True))
        gw = self.gateway_module.ManagedGateway(
            listen_path=base / "listen.sock",
            upstream_path=base / "upstream.sock",
            control_path=base / "control.sock",
            ledger_path=base / "ledger.json",
            session_registry_pid=session_registry_pid,
        )
        gw._tui = mock.Mock()
        return gw

    def test_thread_start_response_merges_session_id(self):
        gw = self._gateway()
        message = {"id": [1, "s1"], "result": {"thread": {"id": "sid-abc"}}}
        with gw._lock:
            gw._latest_transition_sequence = 1
            transition = {
                "epoch": gw._epoch,
                "sequence": 1,
                "method": "thread/start",
                "params": {},
                "prior_thread_id": "",
                "prior_generation": 0,
            }
            gw._track_tui_response_locked(
                ("s", (1, "s1")), "thread/start", {}, message,
                transition=transition,
            )
            pending = gw._pending_registry_fields
        self.assertEqual(pending, {"sessionId": "sid-abc"})

    def test_turn_started_for_binding_thread_merges_busy(self):
        gw = self._gateway()
        gw._binding_thread_id = "sid-abc"
        message = {"method": "turn/started",
                   "params": {"threadId": "sid-abc", "turnId": "t1"}}
        with gw._lock:
            gw._handle_upstream_locked(message)
            pending = gw._pending_registry_fields
        self.assertEqual(pending["status"], "busy")
        self.assertIn("statusUpdatedAt", pending)

    def test_turn_started_for_non_binding_thread_does_not_merge(self):
        gw = self._gateway()
        gw._binding_thread_id = "sid-abc"
        message = {"method": "turn/started",
                   "params": {"threadId": "sid-other", "turnId": "t1"}}
        with gw._lock:
            gw._handle_upstream_locked(message)
            pending = gw._pending_registry_fields
        self.assertIsNone(pending)

    def test_turn_completed_for_binding_thread_merges_idle(self):
        gw = self._gateway()
        gw._binding_thread_id = "sid-abc"
        message = {"method": "turn/completed", "params": {"threadId": "sid-abc"}}
        with gw._lock:
            gw._handle_upstream_locked(message)
            pending = gw._pending_registry_fields
        self.assertEqual(pending["status"], "idle")

    def test_no_writes_without_session_registry_pid(self):
        gw = self._gateway(session_registry_pid=None)
        with gw._lock:
            gw._merge_pending_registry_fields_locked({"status": "busy"})
            pending = gw._pending_registry_fields
        self.assertIsNone(pending)

    def test_flush_writes_only_the_app_server_pid_never_a_client_pid(self):
        gw = self._gateway(session_registry_pid=90001)
        # Patch the module object the gateway itself resolved (`tools.fleet.session_registry`
        # via its own ROOT-relative sys.path insert) — not this test's `fleet.session_registry`
        # import, which sys.modules treats as a distinct instance of the same file.
        with mock.patch.object(self.gateway_module.session_registry, "write") as write_mock:
            gw._flush_registry_write({"status": "busy"})
        write_mock.assert_called_once_with("codex", 90001, {"status": "busy"})
        for call in write_mock.call_args_list:
            self.assertNotEqual(call.args[1], 90003)


class ResumeAliasTest(unittest.TestCase):
    """Prior session ids a resume/fork left behind, derived from the live process's own
    argv. Measured 2026-09-09: pid 2979449 ran
    `… --session-id 6044eb9f-… --fork-session --resume …/2b091520-….jsonl`, and the peer
    ledger held that conversation's receipts under BOTH ids — the older one carrying the
    sender identity the newer one lacked. Nothing on disk records the equivalence."""

    _NEW = "6044eb9f-7983-4c41-9b86-0bb2b70638fa"
    _OLD = "2b091520-0afd-4334-a04f-4511d4debf6e"

    def test_resume_path_stem_becomes_an_alias(self):
        argv = ["claude", "--session-id", self._NEW, "--fork-session",
                "--resume", "/home/u/.claude/projects/-x/%s.jsonl" % self._OLD]
        self.assertEqual(session_registry.session_aliases("claude", 1, argv=argv), [self._OLD])

    def test_bare_resume_id_is_accepted_too(self):
        argv = ["claude", "--resume", self._OLD, "--session-id", self._NEW]
        self.assertEqual(session_registry.session_aliases("claude", 1, argv=argv), [self._OLD])

    def test_a_session_is_never_its_own_alias(self):
        argv = ["claude", "--session-id", self._NEW, "--resume", "%s.jsonl" % self._NEW]
        self.assertEqual(session_registry.session_aliases("claude", 1, argv=argv), [])

    def test_non_uuid_resume_values_are_ignored(self):
        for value in ("--last", "/x/latest.jsonl", "../../etc/passwd", ""):
            argv = ["claude", "--session-id", self._NEW, "--resume", value]
            self.assertEqual(session_registry.session_aliases("claude", 1, argv=argv), [])

    def test_alias_history_is_bounded(self):
        argv = ["claude"]
        for index in range(20):
            argv += ["--resume", "%08x-0000-4000-8000-000000000000" % index]
        aliases = session_registry.session_aliases("claude", 1, argv=argv)
        self.assertEqual(len(aliases), session_registry._MAX_ALIASES)
        self.assertEqual(len(set(aliases)), len(aliases))

    def test_support_is_declared_per_harness_and_gates_derivation(self):
        self.assertEqual(session_registry.alias_support("claude"), "proc-argv")
        for harness in ("codex", "opencode"):
            self.assertEqual(session_registry.alias_support(harness), "not-implemented")
            argv = ["x", "--session-id", self._NEW, "--resume", "%s.jsonl" % self._OLD]
            self.assertEqual(session_registry.session_aliases(harness, 1, argv=argv), [])
        with self.assertRaises(ValueError):
            session_registry.alias_support("nope")

    def test_unreadable_process_is_silence_not_an_error(self):
        self.assertEqual(session_registry.session_aliases("claude", 2 ** 30), [])
        self.assertEqual(session_registry.session_aliases("claude", "not-a-pid"), [])

    def test_claude_write_contract_is_untouched(self):
        """Aliases are derived, never written — the runtime-native record stays read-only."""
        self.assertEqual(session_registry.writer_support("claude"), "runtime-native")
        with self.assertRaises(session_registry.RegistryWriteUnsupported):
            session_registry.write("claude", 1, {"status": "idle"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
