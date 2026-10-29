"""Focused safety regressions for isolated native Codex replay setup."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import socket
import stat
import sqlite3
import subprocess
import sys
import tempfile
import threading
import tomllib
import unittest
from contextlib import ExitStack, contextmanager
from types import SimpleNamespace
from unittest import mock

import codex_parent_completion_replay as replay
import peer_obligations


class HookTrustSetupTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="parent-replay-hook-test-")
        self.root = Path(self.temporary.name)
        self.source_home = self.root / "source-home"
        self.codex_home = self.root / "private-home"
        self.worktree = self.root / "workspace"
        self.source_home.mkdir(mode=0o700)
        self.codex_home.mkdir(mode=0o700)
        self.worktree.mkdir(mode=0o700)
        (self.source_home / "auth.json").write_text("{}\n", encoding="utf-8")
        (self.source_home / "hooks.json").write_text(
            '{"hooks":{"SessionStart":[{"hooks":[{"command":"user-hook"}]}]}}\n',
            encoding="utf-8")
        (self.source_home / "config.toml").write_text(
            '[hooks.state."outside:user-hook"]\ntrusted_hash="sha256:personal"\n',
            encoding="utf-8")
        copied, _auth_source = replay._copy_runtime_config(
            self.source_home, self.codex_home, {})
        self.assertTrue(copied["hooks.json"])
        self.hooks_path = self.codex_home / "hooks.json"
        self.config_path = self.codex_home / "config.toml"

    def tearDown(self):
        self.temporary.cleanup()

    def test_generated_hooks_use_exact_native_hashes_and_private_config(self):
        client = FakeHookTrustClient(self.hooks_path, self.worktree, self.config_path)
        source_bytes = self.hooks_path.read_bytes()
        result = replay._trust_isolated_hooks(
            client, self.codex_home, self.worktree, self.hooks_path)

        generated = json.loads(source_bytes)
        self.assertEqual(set(generated["hooks"]), {"SessionStart", "UserPromptSubmit"})
        self.assertNotIn("user-hook", source_bytes.decode("utf-8"))
        self.assertEqual(result["hooks_json_sha256"], hashlib.sha256(source_bytes).hexdigest())
        self.assertEqual(result["trusted_hook_count"], 2)
        state = tomllib.loads(self.config_path.read_text(encoding="utf-8"))["hooks"]["state"]
        self.assertEqual(set(state), set(client.current_hashes))
        self.assertEqual({key: value["trusted_hash"] for key, value in state.items()},
                         client.current_hashes)
        self.assertTrue(all(key.startswith(str(self.hooks_path) + ":") for key in state))
        self.assertEqual(self.config_path.stat().st_uid, os.geteuid())
        self.assertEqual(self.hooks_path.stat().st_uid, os.geteuid())
        self.assertEqual(stat.S_IMODE(self.config_path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.hooks_path.stat().st_mode), 0o600)
        self.assertEqual(client.list_calls, 2)
        self.assertEqual(client.trust_writes, 1)

    def test_hook_source_change_during_trust_is_rejected(self):
        client = FakeHookTrustClient(self.hooks_path, self.worktree, self.config_path)
        client.mutate_source_on_first_list = True
        with self.assertRaisesRegex(replay.ReplayBlocked, "source-changed-during-trust"):
            replay._trust_isolated_hooks(
                client, self.codex_home, self.worktree, self.hooks_path)

    def test_trusted_thread_starts_after_hash_write_and_probe_unsubscribe(self):
        client = FakeHookTrustClient(self.hooks_path, self.worktree, self.config_path)
        client.thread_ids = iter(("probe-thread", "measured-thread"))

        thread_id, probe_id, trust = replay._start_trusted_replay_thread(
            client, self.codex_home, self.worktree, self.hooks_path)

        self.assertEqual(thread_id, "measured-thread")
        self.assertEqual(probe_id, "probe-thread")
        self.assertEqual(trust["trusted_hook_count"], 2)
        self.assertEqual(client.calls, [
            "thread/start", "hooks/list", "config/batchWrite", "hooks/list",
            "thread/unsubscribe", "thread/start",
        ])
        self.assertTrue(client.trusted)


class FakeHookTrustClient:
    def __init__(self, hooks_path: Path, worktree: Path, config_path: Path):
        self.hooks_path = hooks_path
        self.worktree = worktree
        self.config_path = config_path
        document = json.loads(hooks_path.read_text(encoding="utf-8"))
        source_digest = hashlib.sha256(hooks_path.read_bytes()).hexdigest()
        self.current_hashes = {}
        self.events = {}
        for event, groups in document["hooks"].items():
            for group_index, group in enumerate(groups):
                for hook_index, _hook in enumerate(group["hooks"]):
                    key = f"{hooks_path}:{event.lower()}:{group_index}:{hook_index}"
                    digest = "sha256:" + hashlib.sha256(
                        f"{source_digest}:{event}:{group_index}:{hook_index}".encode()).hexdigest()
                    self.current_hashes[key] = digest
                    self.events[key] = event
        self.list_calls = 0
        self.trust_writes = 0
        self.trusted = False
        self.mutate_source_on_first_list = False
        self.calls = []

    def request(self, method: str, params: dict) -> dict:
        self.calls.append(method)
        if method == "thread/start":
            return {"thread": {"id": next(self.thread_ids)}}
        if method == "thread/unsubscribe":
            return {"status": "ok"}
        if method == "hooks/list":
            self.list_calls += 1
            hooks = [{
                "key": key,
                "currentHash": current_hash,
                "eventName": self.events[key],
                "enabled": True,
                "sourcePath": str(self.hooks_path),
                "trustStatus": "trusted" if self.trusted else "untrusted",
            } for key, current_hash in self.current_hashes.items()]
            response = {"data": [{"cwd": str(self.worktree), "hooks": hooks}]}
            if self.mutate_source_on_first_list and self.list_calls == 1:
                self.hooks_path.write_bytes(self.hooks_path.read_bytes() + b"\n")
            return response
        if method == "config/batchWrite":
            self.trust_writes += 1
            self.assert_private_write(params)
            if any(edit.get("keyPath") != "hooks.state"
                   or edit.get("mergeStrategy") != "upsert"
                   for edit in params["edits"]):
                raise AssertionError("trust write did not use hooks.state upsert")
            with self.config_path.open("a", encoding="utf-8") as stream:
                for edit in params["edits"]:
                    values = edit["value"]
                    for key, value in values.items():
                        stream.write("\n[hooks.state." + json.dumps(key) + "]\n")
                        stream.write("trusted_hash = " + json.dumps(value["trusted_hash"]) + "\n")
            self.trusted = True
            return {"status": "ok"}
        raise AssertionError(f"unexpected App Server method: {method}")

    def assert_private_write(self, params: dict) -> None:
        if (params.get("filePath") != str(self.config_path)
                or params.get("reloadUserConfig") is not True):
            raise AssertionError("trust write did not target the isolated Codex config")
        keys = {key for edit in params.get("edits", [])
                for key in edit.get("value", {})}
        if keys != set(self.current_hashes):
            raise AssertionError("trust write did not match the exact isolated hook source")


class EndpointSymlinkTests(unittest.TestCase):
    def test_start_server_accepts_owned_codex_short_path_socket_symlink(self):
        with tempfile.TemporaryDirectory(prefix="parent-replay-endpoint-test-") as temporary:
            root = Path(temporary)
            codex_home = root / "home" / ".codex"
            endpoint = codex_home / "app-server-control" / "app-server-control.sock"
            worktree = root / "workspace"
            worktree.mkdir(mode=0o700)
            target_dir = root / "daemon"
            target_dir.mkdir(mode=0o700)
            target = target_dir / "server.sock"
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            listener.bind(str(target))

            class Process:
                def poll(self):
                    return None

            process = Process()

            def spawn(*_args, **_kwargs):
                endpoint.symlink_to(target)
                return process

            stderr_path = root / "server.stderr"
            with mock.patch.object(replay.subprocess, "Popen", side_effect=spawn), \
                    mock.patch.object(replay.codex_queue_delivery, "resolve_endpoint",
                                      wraps=replay.codex_queue_delivery.resolve_endpoint) as resolve:
                started = replay._start_server(
                    ["codex", "app-server"], endpoint, worktree, {}, stderr_path)
            try:
                self.assertIs(started, process)
                self.assertTrue(endpoint.is_symlink())
                self.assertEqual(replay.codex_queue_delivery.resolve_endpoint(endpoint), target)
                resolve.assert_called_once_with(endpoint)
            finally:
                process._replay_stderr_file.close()
                listener.close()


class NativeAcceptanceFixtureTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="parent-replay-acceptance-test-")
        self.root = Path(self.temporary.name)
        self.worktree = self.root / "workspace"
        self.worktree.mkdir(mode=0o700)
        self.sentinel = "HEARTING_REPLAY_FIXTURE_ONCE"
        self.fixture = replay._write_native_fixture(self.worktree, self.sentinel)

    def tearDown(self):
        self.temporary.cleanup()

    def _native_queue_fixture(self, thread_id):
        home = self.root / "native-codex-home"
        control = home / "app-server-control"
        sessions = home / "sessions"
        home.mkdir(mode=0o700)
        control.mkdir(mode=0o700)
        sessions.mkdir(mode=0o700)
        rollout = sessions / "fixture.jsonl"
        rollout.write_bytes(b"")
        rollout.chmod(0o600)

        state_path = home / "state_5.sqlite"
        state = sqlite3.connect(state_path)
        state.execute(
            "CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT, history_mode TEXT)")
        state.execute("INSERT INTO threads VALUES (?, ?, 'paginated')",
                      (thread_id, str(rollout)))
        state.commit()
        state.close()
        state_path.chmod(0o600)

        history_path = home / "thread_history_1.sqlite"
        history = sqlite3.connect(history_path)
        history.executescript("""
            CREATE TABLE thread_turns (
                thread_id TEXT NOT NULL, turn_id TEXT NOT NULL, status TEXT,
                PRIMARY KEY(thread_id, turn_id)
            );
            CREATE TABLE thread_items (
                thread_id TEXT NOT NULL, turn_id TEXT NOT NULL, item_id TEXT NOT NULL,
                item_json TEXT NOT NULL, item_type TEXT NOT NULL, rollout_ordinal INTEGER,
                PRIMARY KEY(thread_id, turn_id, item_id)
            );
            CREATE TABLE thread_history_projection_state (
                thread_id TEXT PRIMARY KEY, next_rollout_byte_offset INTEGER,
                next_rollout_ordinal INTEGER
            );
        """)
        history.execute(
            "INSERT INTO thread_history_projection_state VALUES (?, 0, 0)", (thread_id,))
        history.commit()
        history.close()
        history_path.chmod(0o600)
        return control / "app-server-control.sock", history_path, rollout

    def _fixture_tool_trace(self, fixture, sentinel, *, include_action=True):
        fixture_rel = Path(".agent_reports/native-parent-fixture")
        read_command = f"cat {fixture_rel}/*.md {fixture_rel}/fixture-facts.json"
        worktree = fixture["fixture"].parents[1]
        commands = [("read-1", read_command)]
        if include_action:
            commands.append(("action-1",
                             f"python3 -B {fixture_rel}/apply_fixture_followthrough.py {sentinel}"))
        verifier_command = f"python3 -B {fixture_rel}/verify_fixture.py"
        commands.append(("verify-1", verifier_command))
        tools = []
        for tool_id, command in commands:
            result = subprocess.run(["/bin/sh", "-lc", command], cwd=worktree,
                                    capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            tools.append({
                "id": tool_id,
                "command": "/usr/bin/zsh -lc " + json.dumps(command),
                "commandActions": [{"command": command, "type": "unknown"}],
                "cwd": str(worktree),
                "aggregatedOutput": result.stdout,
                "status": "completed",
                "exitCode": 0,
            })
        return tools

    def _valid_tool_trace(self, fixture=None, sentinel=None):
        fixture = fixture or self.fixture
        return self._fixture_tool_trace(
            fixture, sentinel or self.sentinel, include_action=True)

    def _stored_report_fixture(self):
        root = Path(__file__).resolve().parent / "fixtures" / "codex_parent_completion_replay"
        facts = json.loads((root / "first-report-facts.json").read_text(encoding="utf-8"))
        sentinel = facts["after"]["sentinel"]
        fixture = replay._write_native_fixture(self.worktree, sentinel)
        self.assertEqual(fixture["facts"]["report"], facts)
        return root, facts, fixture, sentinel

    def _report_text(self, language="en", report=None, fixture=None):
        facts = (fixture or self.fixture)["facts"]["report"]
        values = facts["synthetic_values"]
        if language == "ko":
            prose = (
                "이미 승인된 후속 작업을 마치기 위해 정확한 결과를 읽고 처리했습니다. "
                "작업 전에는 후속 파일이 없고 승인된 실행 횟수가 0이었지만, 작업 후에는 파일이 "
                "생성되어 실행 횟수가 1이 되었습니다. verify_fixture.py 검증에서 여섯 검사가 모두 "
                "통과했고 실패는 0건입니다. "
                f"합성 커밋 값은 {values['commit']}입니다. 합성 배포 값은 {values['deployment']}입니다. "
                f"합성 서빙 값은 {values['serving']}입니다. "
                "운영 배포 및 서빙 검사는 생략했습니다. 실제 운영 endpoint, 배포, commit, CI, PR "
                "상태를 측정하거나 변경하지 않았습니다. 격리된 Codex 재현은 전달과 보고만 시험했습니다."
            )
        else:
            prose = (
                "I completed the already approved isolated parent follow-through after reading the exact result. "
                "Before the action, the follow-through file was absent and the approved action count was 0; "
                "after the action, the file was present and the approved action count was 1. "
                "Verification with verify_fixture.py passed all six checks with 0 failed. "
                f"The synthetic commit value is {values['commit']}. "
                f"The synthetic deployment value is {values['deployment']}. "
                f"The synthetic serving value is {values['serving']}. "
                "Production deployment and serving checks were skipped. No production endpoint, deployment, "
                "commit, CI run, PR, or operating state was changed or measured. "
                "This isolated Codex replay tests delivery and reporting only."
            )
        return prose + "\nFACTUAL_REPORT_JSON:\n" + json.dumps(
            facts if report is None else report, ensure_ascii=False)

    @contextmanager
    def _mock_native_runtime(self, output, *, report_pass=True, action_applied=True,
                             extra_history=False, history_error=False,
                             assert_d1_before_d2=False, report_mutator=None):
        state = {"history_calls": 0, "queue_sends": [], "fixture": None,
                 "followthrough_snapshot": None, "d1_before_d2": False,
                 "completion_record": None, "runtime": None, "jobs": None}
        real_run = subprocess.run

        def fake_run(command, *args, **kwargs):
            argv = list(command) if isinstance(command, (list, tuple)) else [str(command)]
            if argv and argv[0] == "git":
                return real_run(command, *args, **kwargs)
            if argv and argv[0] == "/mock/codex":
                return subprocess.CompletedProcess(command, 0, stdout="codex-test\n", stderr="")
            if argv and argv[0] == "/bin/sh":
                script = str(argv[-1])
                if ".agent_reports/native-parent-fixture/" not in script:
                    raise AssertionError("fixture helper escaped its isolated worktree")
                return real_run(command, *args, **kwargs)
            raise AssertionError(f"unexpected subprocess in mocked replay: {argv!r}")

        def fake_copy_runtime_config(_source_home, codex_home, _env):
            # Supply disposable test credentials and hook/config files only.
            (codex_home / "auth.json").write_text('{"test-only":true}\n', encoding="utf-8")
            (codex_home / "hooks.json").write_text('{"hooks":{}}\n', encoding="utf-8")
            (codex_home / "config.toml").write_text("[features]\napps = false\n", encoding="utf-8")
            for name in ("auth.json", "hooks.json", "config.toml"):
                (codex_home / name).chmod(0o600)
            return {"auth.json": True, "hooks.json": True, "config.toml": True}, \
                codex_home / "mock-auth-source.json"

        def fake_copy_regular(_source, target, *, private=False):
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            target.write_text('{"test-only":true}\n', encoding="utf-8")
            target.chmod(0o600)
            return True

        original_write_fixture = replay._write_native_fixture

        def write_fixture(worktree, sentinel):
            fixture = original_write_fixture(worktree, sentinel)
            state["fixture"] = fixture
            state["tool_trace"] = self._fixture_tool_trace(
                fixture, sentinel, include_action=action_applied)
            state["followthrough_snapshot"] = (
                fixture["followthrough"].read_bytes()
                if fixture["followthrough"].is_file() else None)
            if report_pass:
                final_text = self._report_text(fixture=fixture)
            else:
                final_text = (
                    "Approved. Before after. Checks pass. Synthetic commit deployment serving. "
                    "Production skipped not measured.\nFACTUAL_REPORT_JSON:\n"
                    + json.dumps(fixture["facts"]["report"], ensure_ascii=False))
            if report_mutator is not None:
                final_text = report_mutator(final_text)
            continuation_id = "delivery-replay-continuation"
            state["continuation_turn"] = {
                "id": "continuation-turn", "status": "completed",
                "items": [{"type": "userMessage", "clientId": continuation_id},
                          {"type": "agentMessage", "text": final_text},
                          *[{"type": "commandExecution", **tool}
                            for tool in state["tool_trace"]]],
            }
            return fixture

        original_fixture_registry = replay._fixture_registry

        def fixture_registry(jobs, thread_id, worktree, sentinel):
            result = original_fixture_registry(jobs, thread_id, worktree, sentinel)
            completion_record = result[3]
            runtime = jobs.resolve().parent
            replay.pending_delivery.claim(
                runtime, thread_id, completion_record["delivery_id"],
                claim_owner="mock-native-hook", lease_seconds=30,
                require_generation_proof=True, live_recipient_generation=(thread_id, "1"),
                jobs=jobs)
            replay.pending_delivery.ack(
                runtime, thread_id, completion_record["delivery_id"],
                acked_by="codex-native-queue:" + completion_record["delivery_id"])
            state.update(completion_record=completion_record, runtime=jobs.resolve().parent,
                         jobs=jobs, thread_id=thread_id)
            return result

        real_obligation_store = peer_obligations.ObligationStore

        class FakeObligationStore(real_obligation_store):
            # Keep real root selection and readonly discovery, including list().
            # Only the exact native-consumer result is supplied by this fixture.
            def __init__(self, root=None, *, allow_state_root_creation=True):
                super().__init__(root, allow_state_root_creation=allow_state_root_creation)

            def get(self, duty_id):
                if self.state_root != state["runtime"]:
                    return super().get(duty_id)
                identity = {"jobs": str(state["jobs"].resolve()),
                            "attempt_id": state["completion_record"]["attempt_ids"][0],
                            "session_id": state["thread_id"],
                            "harness": "codex", "server": "default"}
                if duty_id != peer_obligations.stable_duty_id("registered-batch", identity):
                    return super().get(duty_id)
                return {
                    "state": "complete",
                    "observation": {
                        "native_receiving_turn": {
                            "thread_id": state["thread_id"], "turn_id": "initial-turn",
                            "delivery_id": state["completion_record"]["delivery_id"],
                        },
                        "continuation_id": "delivery-replay-continuation",
                    },
                }

        class FakeRPCClient:
            def request(self, method, _params):
                if method == "turn/start":
                    return {"turn": {"id": "initial-turn"}}
                if method == "thread/resume":
                    return {"thread": {"id": state["thread_id"]}}
                raise AssertionError(f"unexpected mocked app-server request: {method}")

            def close(self):
                pass

        def find_conflict_delivery():
            for record_path in (state["runtime"] / "pending-delivery").glob("*/*.json"):
                try:
                    record = json.loads(record_path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                receipt = record.get("receipt") if isinstance(record, dict) else None
                if isinstance(receipt, dict) and receipt.get("reason") == "terminal-evidence-conflict":
                    return record["delivery_id"]
            raise AssertionError("production did not materialize the conflict delivery")

        def history(_endpoint, _thread_id):
            state["history_calls"] += 1
            call = state["history_calls"]
            initial = {"id": "initial-turn", "status": "interrupted", "items": []}
            continuation = state["continuation_turn"]
            base = [initial, continuation]
            extra = ([{"id": "unrelated-native-turn", "status": "completed",
                       "items": [{"type": "userMessage", "clientId": "unrelated"}]}]
                     if extra_history else [])
            if call <= 3:
                if call == 3 and assert_d1_before_d2:
                    stored = json.loads((output / "replay.json").read_text(encoding="utf-8"))
                    self.assertEqual(
                        stored["replay_cases"]["d1_first_report"]["status"], "FAIL")
                    state["d1_before_d2"] = True
                return base
            if call == 4:
                return base + extra
            if call == 5 and history_error:
                raise RuntimeError("mock native history transport unavailable")
            conflict = {
                "id": "conflict-turn", "status": "completed",
                "items": [{"type": "userMessage", "clientId": find_conflict_delivery()},
                          {"type": "agentMessage", "text": "Conflict notice observed."}],
            }
            turns = base + extra + [conflict]
            if state.get("question_started"):
                turns.append({
                    "id": "question-turn", "status": "completed",
                    "items": [{"type": "agentMessage",
                               "text": "No production deployment was measured; this is fixture-only."}],
                })
            return turns

        def turn_record(_endpoint, _thread_id, turn_id):
            if turn_id == "initial-turn":
                return {"id": turn_id, "status": "interrupted", "items": []}
            if turn_id == "question-turn":
                return {
                    "id": turn_id, "status": "completed",
                    "items": [{"type": "agentMessage",
                               "text": "No production deployment was measured; this is fixture-only."}],
                }
            raise AssertionError(f"unexpected native history lookup: {turn_id}")

        def request(_endpoint, method, _params):
            if method == "turn/start":
                state["question_started"] = True
                return {"turn": {"id": "question-turn"}}
            raise AssertionError(f"unexpected mocked native request: {method}")

        def queue_send(*_args, **_kwargs):
            state["queue_sends"].append("consumed")
            return {"status": "consumed"}

        with ExitStack() as stack:
            masked_daemon_dir = Path("/tmp").resolve() / f"codex-daemon-{os.geteuid()}"
            path_exists = Path.exists

            def isolated_path_exists(path):
                if path == masked_daemon_dir:
                    return False
                return path_exists(path)

            # The runner's masked-daemon sentinel is environmental; all daemon
            # and app-server operations below are independently mocked.
            stack.enter_context(mock.patch.object(Path, "exists", isolated_path_exists))
            stack.enter_context(mock.patch.object(replay.subprocess, "run", side_effect=fake_run))
            stack.enter_context(mock.patch.object(replay.shutil, "which", return_value="/mock/codex"))
            stack.enter_context(mock.patch.object(replay, "_copy_runtime_config",
                                                  side_effect=fake_copy_runtime_config))
            stack.enter_context(mock.patch.object(replay, "_copy_regular",
                                                  side_effect=fake_copy_regular))
            stack.enter_context(mock.patch.object(replay, "_start_server",
                                                  return_value=SimpleNamespace()))
            stack.enter_context(mock.patch.object(replay, "_stop_server"))
            stack.enter_context(mock.patch.object(replay, "_start_trusted_replay_thread",
                return_value=("mock-thread", "mock-probe-thread", {"trusted_hook_count": 2})))
            stack.enter_context(mock.patch.object(replay, "_rpc_session",
                                                  side_effect=lambda _endpoint: FakeRPCClient()))
            stack.enter_context(mock.patch.object(replay, "_request", side_effect=request))
            stack.enter_context(mock.patch.object(replay, "_turn_record", side_effect=turn_record))
            stack.enter_context(mock.patch.object(replay, "_all_turns", side_effect=history))
            stack.enter_context(mock.patch.object(replay, "_fixture_registry",
                                                  side_effect=fixture_registry))
            stack.enter_context(mock.patch.object(replay, "_write_native_fixture",
                                                  side_effect=write_fixture))
            stack.enter_context(mock.patch.object(peer_obligations, "ObligationStore",
                                                  FakeObligationStore))
            stack.enter_context(mock.patch.object(replay.codex_queue_delivery, "send_at_least_once",
                                                  side_effect=queue_send))
            stack.enter_context(mock.patch.object(replay.codex_queue_delivery, "list_queue",
                                                  return_value=[]))
            stack.enter_context(mock.patch.object(replay.time, "sleep", return_value=None))
            thread_errors = stack.enter_context(mock.patch.object(
                threading, "excepthook", wraps=threading.excepthook))
            yield state
            thread_errors.assert_not_called()

    def _assert_case_records(self, stored, case_ids):
        for case_id in case_ids:
            result = stored["replay_cases"][case_id]
            self.assertIn(result["status"], {"PASS", "FAIL", "BLOCKED", "not-run"})
            self.assertTrue(result["reason"])
            self.assertIsInstance(result["checks"], dict)
            self.assertIsInstance(result["evidence"], list)

    def test_run_replay_d1_failure_is_durable_before_d2_and_later_cases_run(self):
        output = self.root / "orchestration-d1-fail"
        output.mkdir(mode=0o700)
        with self._mock_native_runtime(output, report_pass=False,
                                      assert_d1_before_d2=True) as native:
            report = replay._run_replay(output)

        stored = json.loads((output / "replay.json").read_text(encoding="utf-8"))
        cases = stored["replay_cases"]
        self.assertTrue(native["d1_before_d2"])
        self.assertEqual(json.loads(native["followthrough_snapshot"])["approved_action_count"], 1)
        self.assertEqual(cases["d1_first_report"]["status"], "FAIL")
        self.assertEqual(cases["d2_duplicate_cleanup"]["status"], "PASS")
        self.assertEqual(cases["d3_new_conflict"]["status"], "PASS")
        self.assertEqual(cases["direct_question"]["status"], "PASS")
        self.assertEqual(report["verdict"], "FAIL")
        self._assert_case_records(stored, (
            "d1_first_report", "d2_duplicate_cleanup", "d3_new_conflict",
            "direct_question", "overall_acceptance"))

    def test_run_replay_denied_completion_still_evaluates_later_cases(self):
        output = self.root / "orchestration-denied-completion"
        output.mkdir(mode=0o700)
        denied = lambda report: report.replace(
            "I completed the already approved isolated parent follow-through",
            "I did not complete the already approved isolated parent follow-through", 1)
        with self._mock_native_runtime(output, report_mutator=denied,
                                      assert_d1_before_d2=True) as native:
            report = replay._run_replay(output)

        stored = json.loads((output / "replay.json").read_text(encoding="utf-8"))
        cases = stored["replay_cases"]
        self.assertTrue(native["d1_before_d2"])
        self.assertEqual(cases["d1_first_report"]["status"], "FAIL")
        self.assertFalse(cases["d1_first_report"]["checks"]
                         ["report_has_substantive_closing_prose"])
        self.assertEqual(cases["d2_duplicate_cleanup"]["status"], "PASS")
        self.assertEqual(cases["d3_new_conflict"]["status"], "PASS")
        self.assertEqual(cases["direct_question"]["status"], "PASS")
        self.assertEqual(report["verdict"], "FAIL")

    def test_run_replay_missing_first_report_still_evaluates_later_cases(self):
        output = self.root / "orchestration-empty-report"
        output.mkdir(mode=0o700)
        with self._mock_native_runtime(output, assert_d1_before_d2=True) as native:
            observed_history = replay._all_turns.side_effect

            def empty_report_history(*args):
                turns = observed_history(*args)
                for turn in turns:
                    if turn.get("id") == "continuation-turn":
                        turn["items"] = [item for item in turn["items"]
                                         if item.get("type") != "agentMessage"]
                return turns

            replay._all_turns.side_effect = empty_report_history
            report = replay._run_replay(output)

        stored = json.loads((output / "replay.json").read_text(encoding="utf-8"))
        cases = stored["replay_cases"]
        self.assertTrue(native["d1_before_d2"])
        self.assertEqual((output / "turn-output.txt").read_text(), "\n")
        self.assertEqual(cases["d1_first_report"]["status"], "FAIL")
        self.assertFalse(cases["d1_first_report"]["checks"]["report_matches_fixture_facts"])
        for case_id in ("d2_duplicate_cleanup", "d3_new_conflict", "direct_question"):
            self.assertEqual(cases[case_id]["status"], "PASS")
        self.assertEqual(report["verdict"], "FAIL")
        self._assert_case_records(stored, (
            "d1_first_report", "d2_duplicate_cleanup", "d3_new_conflict",
            "direct_question", "overall_acceptance"))

    def test_run_replay_d2_failure_does_not_skip_independent_later_cases(self):
        output = self.root / "orchestration-d2-fail"
        output.mkdir(mode=0o700)
        with self._mock_native_runtime(output, extra_history=True) as _native:
            report = replay._run_replay(output)

        stored = json.loads((output / "replay.json").read_text(encoding="utf-8"))
        cases = stored["replay_cases"]
        self.assertEqual(cases["d1_first_report"]["status"], "PASS")
        self.assertEqual(cases["d2_duplicate_cleanup"]["status"], "FAIL")
        self.assertIn("cleanup_created_no_extra_turn", cases["d2_duplicate_cleanup"]["reason"])
        self.assertEqual(cases["d3_new_conflict"]["status"], "PASS")
        self.assertEqual(cases["direct_question"]["status"], "PASS")
        self.assertEqual(report["verdict"], "FAIL")

    def test_run_replay_missing_followthrough_blocks_only_dependent_judgments(self):
        output = self.root / "orchestration-missing-prerequisite"
        output.mkdir(mode=0o700)
        with self._mock_native_runtime(output, action_applied=False) as native:
            report = replay._run_replay(output)

        stored = json.loads((output / "replay.json").read_text(encoding="utf-8"))
        cases = stored["replay_cases"]
        self.assertIsNone(native["followthrough_snapshot"])
        self.assertEqual(cases["d1_first_report"]["status"], "FAIL")
        self.assertEqual(cases["d2_duplicate_cleanup"]["status"], "PASS")
        self.assertEqual(cases["d3_new_conflict"]["status"], "BLOCKED")
        self.assertIn("follow-through state absent", cases["d3_new_conflict"]["reason"])
        self.assertEqual(cases["direct_question"]["status"], "BLOCKED")
        self.assertIn("follow-through state absent", cases["direct_question"]["reason"])
        self.assertEqual(report["verdict"], "FAIL")

    def test_run_replay_later_transport_error_keeps_failure_and_names_unrun_cases(self):
        output = self.root / "orchestration-transport-error"
        output.mkdir(mode=0o700)
        with self._mock_native_runtime(output, report_pass=False,
                                      history_error=True) as _native:
            report = replay._run_replay(output)

        stored = json.loads((output / "replay.json").read_text(encoding="utf-8"))
        cases = stored["replay_cases"]
        self.assertEqual(cases["d1_first_report"]["status"], "FAIL")
        self.assertEqual(cases["d2_duplicate_cleanup"]["status"], "PASS")
        for case_id in ("d3_new_conflict", "direct_question", "overall_acceptance"):
            self.assertEqual(cases[case_id]["status"], "not-run")
            self.assertIn("mock native history transport unavailable", cases[case_id]["reason"])
        self.assertEqual(report["verdict"], "FAIL")

    def test_run_replay_persists_all_pass_aggregate(self):
        output = self.root / "orchestration-all-pass"
        output.mkdir(mode=0o700)
        with self._mock_native_runtime(output) as _native:
            report = replay._run_replay(output)

        stored = json.loads((output / "replay.json").read_text(encoding="utf-8"))
        self.assertEqual(report["verdict"], "PASS")
        self.assertEqual(stored["verdict"], "PASS")
        for case_id in ("d1_first_report", "d2_duplicate_cleanup", "d3_new_conflict",
                        "direct_question", "overall_acceptance"):
            self.assertEqual(stored["replay_cases"][case_id]["status"], "PASS")

    def test_turn_items_keep_native_cwd_and_command_action_metadata(self):
        command = "cat .agent_reports/native-parent-fixture/*.md"
        messages, tools = replay._turn_items({"items": [
            {"type": "agentMessage", "text": "done"},
            {"type": "commandExecution", "id": "native-1", "command": "/usr/bin/zsh -lc 'pwd'",
             "commandActions": [{"type": "unknown", "command": command}],
             "cwd": str(self.worktree), "aggregatedOutput": "", "exitCode": 0,
             "status": "completed"},
        ]})
        self.assertEqual(messages, ["done"])
        self.assertEqual(tools[0]["cwd"], str(self.worktree))
        self.assertEqual(tools[0]["commandActions"][0]["command"], command)

    def test_relative_glob_and_recorded_cwd_prove_each_exact_artifact_read(self):
        tools = self._valid_tool_trace()
        facts = self.fixture["facts"]["report"]
        checks = replay._first_report_checks(self._report_text(), tools, self.fixture)
        self.assertTrue(checks["every_exact_artifact_was_read"])
        self.assertTrue(all(checks["artifact_reads"].values()))
        self.assertTrue(all(evidence[0]["cwd_source"] == "commandExecution"
                            for evidence in checks["artifact_read_evidence"].values()))

        wrong_cwd = [{**item, "cwd": str(self.root)} for item in tools]
        checks = replay._first_report_checks(self._report_text(), wrong_cwd, self.fixture)
        self.assertFalse(checks["every_exact_artifact_was_read"])
        self.assertFalse(checks["fixture_verifier_passed_in_native_tool_trace"])
        self.assertFalse(checks["one_approved_followthrough_tool_invocation"])

    def test_marker_only_and_failed_verifier_do_not_satisfy_execution_evidence(self):
        fake = {
            "id": "fake-1", "command": "/usr/bin/zsh -lc 'echo FIXTURE_VERIFICATION=PASS checks=6 failed=0'",
            "commandActions": [{"type": "unknown",
                                "command": "echo FIXTURE_VERIFICATION=PASS checks=6 failed=0"}],
            "cwd": str(self.worktree),
            "aggregatedOutput": "FIXTURE_ID=" + self.fixture["facts"]["fixture_id"]
                              + "\nFIXTURE_VERIFICATION=PASS checks=6 failed=0",
            "status": "completed", "exitCode": 0,
        }
        checks = replay._first_report_checks("", [fake], self.fixture)
        self.assertFalse(checks["every_exact_artifact_was_read"])
        self.assertFalse(checks["fixture_verifier_passed_in_native_tool_trace"])

        code_argument = {
            **fake,
            "commandActions": [{
                "type": "unknown",
                "command": ("python3 -c 'print(1)' "
                            ".agent_reports/native-parent-fixture/verify_fixture.py"),
            }],
            "aggregatedOutput": "FIXTURE_VERIFICATION=PASS checks=6 failed=0",
        }
        checks = replay._first_report_checks("", [code_argument], self.fixture)
        self.assertFalse(checks["fixture_verifier_passed_in_native_tool_trace"])

        tools = self._valid_tool_trace()
        verifier = next(item for item in tools if item["id"] == "verify-1")
        verifier["status"] = "failed"
        verifier["exitCode"] = 1
        checks = replay._first_report_checks("", tools, self.fixture)
        self.assertFalse(checks["fixture_verifier_passed_in_native_tool_trace"])

    def test_case_sequence_helper_continues_after_first_report_failure(self):
        output = self.root / "evidence"
        output.mkdir(mode=0o700)
        report = {"schema_version": 1, "replay_cases": {}}
        valid_trace = self._valid_tool_trace()
        filler = ("Approved. Before after. Checks pass. Synthetic commit deployment serving. "
                  "Production skipped not measured.\nFACTUAL_REPORT_JSON:\n"
                  + json.dumps(self.fixture["facts"]["report"], ensure_ascii=False))
        first_report_observation = replay._first_report_checks(filler, valid_trace, self.fixture)

        class FakeNativeTransport:
            def __init__(transport_self):
                transport_self.calls = []

            def first_report(transport_self):
                transport_self.calls.append("d1_first_report")
                return {key: value for key, value in first_report_observation.items()
                        if isinstance(value, bool)}

            def cleanup_and_duplicate(transport_self):
                transport_self.calls.append("d2_duplicate_cleanup")
                persisted = json.loads((output / "replay.json").read_text())
                self.assertEqual(persisted["replay_cases"]["d1_first_report"]["status"], "FAIL")
                return {
                    "cleanup_is_current": False,
                    "cleanup_text": "Late deadline retains cleanup observation only",
                    "duplicate_state": "acked", "duplicate_kept_exact_ack": True,
                    "cleanup_courier_state": "rejected",
                    "cleanup_courier_reason": "supervision-resolved",
                    "old_ack_preserved": True, "no_extra_turn": False,
                    "no_cleanup_queue_item": True, "no_queued_duplicate": True,
                }

            def new_conflict(transport_self):
                transport_self.calls.append("d3_new_conflict")
                return {"conflict_fixture_valid": True, "notice_is_distinct_current": True,
                        "notice_ack_observed": True, "notice_turn_status": "completed",
                        "notice_has_response": True, "notice_occurrences": 1,
                        "old_ack_preserved": True, "no_duplicate_action_or_response": True}

            def direct_question(transport_self):
                transport_self.calls.append("direct_question")
                return {"answer": "No production deployment was measured; result is fixture only.",
                        "followthrough_unchanged": True, "action_invocations": 0}

        transport = FakeNativeTransport()
        replay._run_replay_case_sequence(report, output, [
            {"case_id": "d1_first_report", "observe": transport.first_report,
             "evidence": ["first-report-checks.json"]},
            {"case_id": "d2_duplicate_cleanup", "observe": transport.cleanup_and_duplicate,
             "evidence": ["late-cleanup-courier.json"]},
            {"case_id": "d3_new_conflict", "observe": transport.new_conflict,
             "evidence": ["late-conflict-notice.json"]},
            {"case_id": "direct_question", "observe": transport.direct_question,
             "evidence": ["direct-question-turn.json"]},
        ])
        stored = json.loads((output / "replay.json").read_text())
        self.assertEqual(transport.calls, ["d1_first_report", "d2_duplicate_cleanup",
                                           "d3_new_conflict", "direct_question"])
        self.assertEqual(stored["replay_cases"]["d1_first_report"]["status"], "FAIL")
        self.assertEqual(stored["replay_cases"]["d2_duplicate_cleanup"]["status"], "FAIL")
        self.assertEqual(stored["replay_cases"]["d3_new_conflict"]["status"], "PASS")
        self.assertEqual(stored["replay_cases"]["direct_question"]["status"], "PASS")
        self.assertIn("cleanup_created_no_extra_turn",
                      stored["replay_cases"]["d2_duplicate_cleanup"]["reason"])
        self.assertEqual(stored["replay_cases"]["d1_first_report"]["evidence"],
                         ["first-report-checks.json"])

    def test_case_sequence_helper_blocks_missing_prerequisite_without_fabricating_state(self):
        output = self.root / "missing-prerequisite"
        output.mkdir(mode=0o700)
        report = {"schema_version": 1, "replay_cases": {}}
        state = {"followthrough": None}
        d1_checks = {key: value for key, value in
                     replay._first_report_checks(self._report_text(), self._valid_tool_trace(),
                                                 self.fixture).items()
                     if isinstance(value, bool)}

        class IndependentTransport:
            def d3(self):
                return {"conflict_fixture_valid": True, "notice_is_distinct_current": True,
                        "notice_ack_observed": True, "notice_turn_status": "completed",
                        "notice_has_response": True, "notice_occurrences": 1,
                        "old_ack_preserved": True, "no_duplicate_action_or_response": True}

            def question(self):
                return {"answer": "No production result; this is fixture-only.",
                        "followthrough_unchanged": True, "action_invocations": 0}

        transport = IndependentTransport()
        replay._run_replay_case_sequence(report, output, [
            {"case_id": "d1_first_report", "observe": lambda: d1_checks},
            {"case_id": "d2_duplicate_cleanup", "observe": lambda: self.fail("d2 ran without state"),
             "prerequisites": [("followthrough state", lambda: state["followthrough"] is not None)]},
            {"case_id": "d3_new_conflict", "observe": transport.d3},
            {"case_id": "direct_question", "observe": transport.question},
        ])
        stored = json.loads((output / "replay.json").read_text())
        self.assertIsNone(state["followthrough"])
        self.assertEqual(stored["replay_cases"]["d2_duplicate_cleanup"]["status"], "BLOCKED")
        self.assertIn("missing prerequisite state",
                      stored["replay_cases"]["d2_duplicate_cleanup"]["reason"])
        self.assertEqual(stored["replay_cases"]["d3_new_conflict"]["status"], "PASS")
        self.assertEqual(stored["replay_cases"]["direct_question"]["status"], "PASS")

    def test_case_sequence_helper_preserves_failure_after_transport_exception(self):
        output = self.root / "failure-evidence"
        output.mkdir(mode=0o700)
        report = {"schema_version": 1, "replay_cases": {}}
        replay._run_replay_case_sequence(report, output, [
            {"case_id": "d1_first_report", "observe": lambda: {"exact_facts": False}},
            {"case_id": "d2_duplicate_cleanup",
             "observe": lambda: (_ for _ in ()).throw(RuntimeError("queue unavailable"))},
            {"case_id": "d3_new_conflict", "observe": lambda: self.fail("transport already failed")},
            {"case_id": "direct_question", "observe": lambda: self.fail("transport already failed")},
        ])
        self.assertEqual(json.loads((output / "replay.json").read_text())[
            "replay_cases"]["d1_first_report"]["status"], "FAIL")
        finished = replay._finish_after_runtime_error(report, output, "BLOCKED", "queue unavailable")
        self.assertEqual(finished["verdict"], "FAIL")
        self.assertEqual(finished["replay_cases"]["d1_first_report"]["status"], "FAIL")
        self.assertEqual(finished["replay_cases"]["d2_duplicate_cleanup"]["status"], "BLOCKED")
        self.assertEqual(finished["replay_cases"]["d3_new_conflict"]["status"], "not-run")
        self.assertIn("queue unavailable", finished["replay_cases"]["d2_duplicate_cleanup"]["reason"])

    def test_case_sequence_helper_aggregates_all_evaluators_pass(self):
        output = self.root / "all-pass"
        output.mkdir(mode=0o700)
        report = {"schema_version": 1, "replay_cases": {}}
        first = replay._first_report_checks(self._report_text(), self._valid_tool_trace(), self.fixture)
        d1 = {key: value for key, value in first.items() if isinstance(value, bool)}
        replay._run_replay_case_sequence(report, output, [
            {"case_id": "d1_first_report", "observe": lambda: d1},
            {"case_id": "d2_duplicate_cleanup", "observe": lambda: {
                "cleanup_is_current": False,
                "cleanup_text": "Late deadline retains cleanup observation only",
                "duplicate_state": "acked", "duplicate_kept_exact_ack": True,
                "cleanup_courier_state": "rejected", "cleanup_courier_reason": "supervision-resolved",
                "old_ack_preserved": True, "no_extra_turn": True,
                "no_cleanup_queue_item": True, "no_queued_duplicate": True}},
            {"case_id": "d3_new_conflict", "observe": lambda: {
                "conflict_fixture_valid": True, "notice_is_distinct_current": True,
                "notice_ack_observed": True, "notice_turn_status": "completed",
                "notice_has_response": True, "notice_occurrences": 1,
                "old_ack_preserved": True, "no_duplicate_action_or_response": True}},
            {"case_id": "direct_question", "observe": lambda: {
                "answer": "No production deployment was measured; fixture only.",
                "followthrough_unchanged": True, "action_invocations": 0}},
        ])
        stored = json.loads((output / "replay.json").read_text())
        self.assertTrue(all(stored["replay_cases"][case]["status"] == "PASS"
                            for case in ("d1_first_report", "d2_duplicate_cleanup",
                                         "d3_new_conflict", "direct_question")))
        self.assertEqual(replay._aggregate_replay_status(
            report, {"native_acceptance": True}), "PASS")

    def test_ordered_synthetic_facts_and_exact_native_report_are_required(self):
        facts = self.fixture["facts"]
        self.assertEqual([item["result"] for item in facts["events"]],
                         ["FAIL", "FIXED", "PASS", "SUCCESS", "PASS", "COMPLETE"])
        self.assertTrue(facts["synthetic_values"]["commit"].startswith("synthetic-only:"))
        self.assertTrue(facts["synthetic_values"]["deployment"].startswith("synthetic-only:"))
        self.assertIn("not probed", facts["synthetic_values"]["serving"])
        self.assertEqual(facts["report"]["before"]["approved_action_count"], 0)
        self.assertEqual(facts["report"]["after"]["approved_action_count"], 1)
        self.assertTrue(facts["report"]["skipped"])
        self.assertTrue(facts["report"]["limits"])

        tools = self._valid_tool_trace()
        text = self._report_text()
        checks = replay._first_report_checks(text, tools, self.fixture)
        for key in ("report_matches_fixture_facts", "report_has_substantive_closing_prose",
                    "every_exact_artifact_was_read", "fixture_verifier_passed_in_native_tool_trace",
                    "one_approved_followthrough_tool_invocation",
                    "followthrough_state_matches_one_time_fixture_action"):
            self.assertTrue(checks[key], key)
        self.assertTrue(all(checks["artifact_reads"].values()))
        self.assertTrue(all(checks["prose_content_checks"].values()))
        self.assertTrue(all(checks["synthetic_value_checks"].values()))
        self.assertEqual(len(checks["artifact_read_evidence"]), 7)
        self.assertTrue(checks["verifier_script_unchanged"])
        self.assertTrue(checks["followthrough_script_unchanged"])

    def test_saved_original_first_report_is_positive_with_independent_facts(self):
        root, facts, fixture, sentinel = self._stored_report_fixture()
        report_bytes = (root / "first-report-positive.txt").read_bytes()
        self.assertEqual(hashlib.sha256(report_bytes).hexdigest(),
                         "253c30c510e0ffc6b0c382b841b12d7a3557bdd0a53c27fce3968eb8675a0aab")
        report = report_bytes.decode("utf-8")
        self.assertEqual(replay._factual_report_object(report), facts)
        checks = replay._first_report_checks(
            report, self._fixture_tool_trace(fixture, sentinel), fixture)
        for key in ("report_matches_fixture_facts", "report_has_substantive_closing_prose",
                    "every_exact_artifact_was_read", "fixture_verifier_passed_in_native_tool_trace",
                    "one_approved_followthrough_tool_invocation",
                    "followthrough_state_matches_one_time_fixture_action"):
            self.assertTrue(checks[key], key)
        self.assertTrue(all(checks["prose_content_checks"].values()))
        self.assertTrue(all(checks["synthetic_value_checks"].values()))

    def test_saved_original_first_report_accepts_fenced_json_whitespace_variants(self):
        root, facts, fixture, sentinel = self._stored_report_fixture()
        report_bytes = (root / "first-report-positive.txt").read_bytes()
        self.assertEqual(hashlib.sha256(report_bytes).hexdigest(),
                         "253c30c510e0ffc6b0c382b841b12d7a3557bdd0a53c27fce3968eb8675a0aab")
        report = report_bytes.decode("utf-8")
        fence_open = "FACTUAL_REPORT_JSON:\n```json\n"
        self.assertEqual(report.count(fence_open), 1)
        tools = self._fixture_tool_trace(fixture, sentinel)

        for label, leading_whitespace in (("blank line", "\n"), ("indentation", "  ")):
            with self.subTest(leading_whitespace=label):
                varied_report = report.replace(
                    fence_open, fence_open + leading_whitespace, 1)
                self.assertEqual(
                    varied_report.replace(fence_open + leading_whitespace, fence_open, 1),
                    report)
                self.assertEqual(replay._factual_report_object(varied_report), facts)
                checks = replay._first_report_checks(varied_report, tools, fixture)
                for key in (
                        "report_matches_fixture_facts", "report_has_substantive_closing_prose",
                        "every_exact_artifact_was_read",
                        "fixture_verifier_passed_in_native_tool_trace",
                        "one_approved_followthrough_tool_invocation",
                        "followthrough_state_matches_one_time_fixture_action"):
                    self.assertTrue(checks[key], key)
                self.assertTrue(all(checks["prose_content_checks"].values()))
                self.assertTrue(all(checks["synthetic_value_checks"].values()))

    def test_saved_positive_semantic_mutations_keep_facts_and_tool_trace_constant(self):
        root, facts, fixture, sentinel = self._stored_report_fixture()
        report = (root / "first-report-positive.txt").read_bytes().decode("utf-8")
        marker = "\nFACTUAL_REPORT_JSON:\n"
        prose, json_tail = report.split(marker, 1)
        tools = self._fixture_tool_trace(fixture, sentinel)

        def check_mutation(name, mutated_prose, key, expected):
            with self.subTest(name=name):
                checks = replay._first_report_checks(mutated_prose + marker + json_tail,
                                                     tools, fixture)
                self.assertTrue(checks["report_matches_fixture_facts"])
                self.assertTrue(checks["every_exact_artifact_was_read"])
                self.assertTrue(checks["fixture_verifier_passed_in_native_tool_trace"])
                self.assertTrue(checks["one_approved_followthrough_tool_invocation"])
                self.assertTrue(checks["followthrough_state_matches_one_time_fixture_action"])
                self.assertEqual(checks["prose_content_checks"].get(key,
                                                                     checks["synthetic_value_checks"].get(
                                                                         key.removeprefix("id_"))), expected)
                self.assertEqual(checks["report_has_substantive_closing_prose"], expected)
                return checks

        alternatives = (
            ("before did not exist", prose.replace(
                "Before: no follow-through file, action count 0.",
                "Before: the follow-through file did not exist, action count 0."),
             "describes_before_state_and_zero_actions"),
            ("before was not present", prose.replace(
                "Before: no follow-through file, action count 0.",
                "Before: the follow-through file was not present, action count 0."),
             "describes_before_state_and_zero_actions"),
            ("six passed without zero failures", prose.replace(
                "passing 6 checks with 0 failures", "passing all 6 checks"),
             "reports_six_check_verification_result"),
        )
        for name, mutated, key in alternatives:
            self.assertNotEqual(mutated, prose)
            checks = check_mutation(name, mutated, key, True)
            self.assertTrue(all(checks["prose_content_checks"].values()))
            self.assertTrue(all(checks["synthetic_value_checks"].values()))

        unrelated_negation = prose.replace(
            "All seven artifacts were read in the specified order.",
            "An unrelated task did not complete. All seven artifacts were read in the specified order.", 1)
        unrelated_checks = check_mutation("unrelated negation does not deny follow-through",
                                          unrelated_negation, "explains_fixture_reason", True)
        self.assertTrue(all(unrelated_checks["prose_content_checks"].values()))

        values = facts["synthetic_values"]
        ids = {key: replay._expected_identifier(key, value) for key, value in values.items()}
        group_qualified = (prose.replace(
            "Synthetic commit:", "All displayed values are synthetic fixture data. Commit:", 1)
            .replace("Synthetic deployment:", "Deployment:", 1)
            .replace("Synthetic serving:", "Serving:", 1))
        grouped = check_mutation("one qualifier for adjacent role values", group_qualified,
                                 "names_actual_synthetic_values", True)
        self.assertEqual(grouped["synthetic_value_checks"],
                         {key: True for key in ("commit", "deployment", "serving")})
        unqualified = group_qualified.replace("All displayed values are synthetic fixture data. ", "", 1)
        unqualified_checks = replay._first_report_checks(unqualified + marker + json_tail,
                                                         tools, fixture)
        self.assertEqual(unqualified_checks["synthetic_value_checks"],
                         {key: False for key in ids})
        self.assertFalse(unqualified_checks["report_has_substantive_closing_prose"])

        denied = prose.replace("Completed the approved synthetic fixture follow-through",
                               "Did not complete the approved synthetic fixture follow-through", 1)
        denied_checks = replay._first_report_checks(denied + marker + json_tail, tools, fixture)
        self.assertTrue(denied_checks["report_matches_fixture_facts"])
        self.assertTrue(denied_checks["every_exact_artifact_was_read"])
        self.assertTrue(denied_checks["fixture_verifier_passed_in_native_tool_trace"])
        self.assertFalse(denied_checks["prose_content_checks"]["explains_fixture_reason"])
        self.assertFalse(denied_checks["report_has_substantive_closing_prose"])

        operating_change = prose.replace(
            "No real commit, PR, CI, deployment, production serving, or operating state was changed or measured.",
            "Production serving was changed, but no CI run, PR, or deployment was changed or measured.", 1)
        operating_checks = replay._first_report_checks(
            operating_change + marker + json_tail, tools, fixture)
        self.assertTrue(operating_checks["report_matches_fixture_facts"])
        self.assertTrue(operating_checks["fixture_verifier_passed_in_native_tool_trace"])
        self.assertFalse(operating_checks["prose_content_checks"]
                         ["states_fixture_skip_and_operating_limit"])
        self.assertFalse(operating_checks["report_has_substantive_closing_prose"])

        double_negative = prose.replace(
            "Before: no follow-through file, action count 0.",
            "Before: the follow-through file was not absent, action count 0.", 1)
        double_negative_checks = replay._first_report_checks(
            double_negative + marker + json_tail, tools, fixture)
        self.assertFalse(double_negative_checks["prose_content_checks"]
                         ["describes_before_state_and_zero_actions"])
        reversed_state = prose.replace("Before: no follow-through file",
                                       "Before: the follow-through file was present", 1)
        reversed_checks = replay._first_report_checks(reversed_state + marker + json_tail,
                                                      tools, fixture)
        self.assertFalse(reversed_checks["prose_content_checks"]
                         ["describes_before_state_and_zero_actions"])

        failed_verification = prose.replace("passing 6 checks with 0 failures",
                                            "failing 2 of 6 checks", 1)
        failed_checks = replay._first_report_checks(failed_verification + marker + json_tail,
                                                    tools, fixture)
        self.assertFalse(failed_checks["prose_content_checks"]
                         ["reports_six_check_verification_result"])
        wrong_count = prose.replace("passing 6 checks with 0 failures",
                                    "passing 5 checks with 0 failures", 1)
        wrong_count_checks = replay._first_report_checks(wrong_count + marker + json_tail,
                                                         tools, fixture)
        self.assertFalse(wrong_count_checks["prose_content_checks"]
                         ["reports_six_check_verification_result"])

    def test_korean_absence_double_negation_and_denied_completion(self):
        marker = "\nFACTUAL_REPORT_JSON:\n"
        report = self._report_text(language="ko")
        prose, json_tail = report.split(marker, 1)
        tools = self._valid_tool_trace()

        for name, old, new in (
            ("did not exist", "후속 파일이 없고", "후속 파일이 존재하지 않았고"),
            ("was not present", "후속 파일이 없고", "후속 파일이 있지 않았고"),
        ):
            with self.subTest(name=name):
                mutated = prose.replace(old, new, 1)
                checks = replay._first_report_checks(mutated + marker + json_tail,
                                                     tools, self.fixture)
                self.assertTrue(checks["report_matches_fixture_facts"])
                self.assertTrue(checks["prose_content_checks"]
                                ["describes_before_state_and_zero_actions"])
                self.assertTrue(checks["report_has_substantive_closing_prose"])

        for name, replacement in (
            ("없지 않", "후속 파일이 없지 않았고"),
            ("존재하지 않는 것은 아니다", "후속 파일이 존재하지 않는 것은 아니고"),
        ):
            with self.subTest(name=name):
                mutated = prose.replace("후속 파일이 없고", replacement, 1)
                checks = replay._first_report_checks(mutated + marker + json_tail,
                                                     tools, self.fixture)
                self.assertFalse(checks["prose_content_checks"]
                                 ["describes_before_state_and_zero_actions"])
                self.assertFalse(checks["report_has_substantive_closing_prose"])

        denied = prose.replace(
            "이미 승인된 후속 작업을 마치기 위해 정확한 결과를 읽고 처리했습니다.",
            "이미 승인된 후속 작업을 완료하지 않았기 때문에 정확한 결과를 읽고 처리했습니다.", 1)
        denied_checks = replay._first_report_checks(denied + marker + json_tail,
                                                    tools, self.fixture)
        self.assertFalse(denied_checks["prose_content_checks"]["explains_fixture_reason"])
        self.assertFalse(denied_checks["report_has_substantive_closing_prose"])

        operating_change = prose.replace(
            "실제 운영 endpoint, 배포, commit, CI, PR 상태를 측정하거나 변경하지 않았습니다.",
            "실제 운영 serving 상태는 변경되었지만 CI, PR 및 배포는 측정하거나 변경하지 않았습니다.", 1)
        operating_checks = replay._first_report_checks(
            operating_change + marker + json_tail, tools, self.fixture)
        self.assertFalse(operating_checks["prose_content_checks"]
                         ["states_fixture_skip_and_operating_limit"])

    def test_saved_previous_short_report_remains_negative_with_valid_facts(self):
        root, facts, fixture, sentinel = self._stored_report_fixture()
        report_bytes = (root / "first-report-negative.txt").read_bytes()
        self.assertEqual(hashlib.sha256(report_bytes).hexdigest(),
                         "cf8d6b7ef114cf9e137d43c7f4fcc9875b99e8ec8d919d94bf82f82767a108c3")
        report = report_bytes.decode("utf-8")
        tools = self._fixture_tool_trace(fixture, sentinel)
        checks = replay._first_report_checks(report, tools, fixture)
        self.assertIsNone(replay._factual_report_object(report))
        self.assertFalse(checks["report_matches_fixture_facts"])
        self.assertFalse(checks["report_has_substantive_closing_prose"])

        with_valid_facts = (report.rstrip() + "\nFACTUAL_REPORT_JSON:\n"
                            + json.dumps(facts, ensure_ascii=False))
        checks = replay._first_report_checks(with_valid_facts, tools, fixture)
        self.assertTrue(checks["report_matches_fixture_facts"])
        self.assertFalse(checks["report_has_substantive_closing_prose"])
        self.assertFalse(checks["prose_content_checks"]["names_actual_synthetic_values"])

    def test_factual_report_accepts_plain_or_fenced_json_and_rejects_invalid_payloads(self):
        facts = self.fixture["facts"]["report"]
        plain = "FACTUAL_REPORT_JSON:\n" + json.dumps(facts, ensure_ascii=False)
        fenced = "FACTUAL_REPORT_JSON:\n```json\n" + json.dumps(facts, indent=2) + "\n```\n"
        fenced_blank_line = "FACTUAL_REPORT_JSON:\n```json\n\n" + json.dumps(facts) + "\n```"
        fenced_indented = "FACTUAL_REPORT_JSON:\n```json\n  " + json.dumps(facts) + "\n```"
        bare_fence = "FACTUAL_REPORT_JSON:\n```\n" + json.dumps(facts) + "\n```"
        for text in (plain, fenced, fenced_blank_line, fenced_indented, bare_fence):
            with self.subTest(text=text[:40]):
                self.assertEqual(replay._factual_report_object(text), facts)
        for payload in ("```json\n{\n```", "```json\n{}", "[1, 2]", "null", "{} trailing"):
            with self.subTest(payload=payload):
                self.assertIsNone(replay._factual_report_object(
                    "FACTUAL_REPORT_JSON:\n" + payload))

    def test_natural_labels_and_unadorned_identifiers_are_accepted(self):
        facts = self.fixture["facts"]["report"]
        values = facts["synthetic_values"]
        identifiers = {key: replay._expected_identifier(key, value)
                       for key, value in values.items()}
        prose = (
            "I completed the approved follow-through to finish the isolated parent replay.\n"
            "Before the action, no follow-through file existed and the action count was zero.\n"
            "After the action, the follow-through file exists and the action count is one.\n"
            "Verification passed all six checks with zero failures.\n"
            f"Synthetic commit: `{identifiers['commit']}`.\n"
            f"Synthetic deployment: `{identifiers['deployment']}`.\n"
            f"Synthetic serving: `{identifiers['serving']}` (not probed).\n"
            "Production deployment and serving checks were skipped.\n"
            "No production operating state was changed or measured.\n"
            "This isolated Codex replay tests delivery and reporting only."
        )
        report = prose + "\nFACTUAL_REPORT_JSON:\n" + json.dumps(facts, ensure_ascii=False)
        checks = replay._first_report_checks(report, self._valid_tool_trace(), self.fixture)
        self.assertTrue(checks["report_has_substantive_closing_prose"])
        self.assertTrue(all(checks["prose_content_checks"].values()))
        self.assertTrue(all(checks["synthetic_value_checks"].values()))

    def test_identifier_negation_mutation_and_reversed_states_are_rejected(self):
        facts = self.fixture["facts"]["report"]
        tools = self._valid_tool_trace()
        values = facts["synthetic_values"]
        deployment_id = replay._expected_identifier("deployment", values["deployment"])
        baseline = self._report_text()

        wrong_id = baseline.replace(deployment_id, deployment_id + "-wrong", 1)
        checks = replay._first_report_checks(wrong_id, tools, self.fixture)
        self.assertTrue(checks["report_matches_fixture_facts"])
        self.assertFalse(checks["synthetic_value_checks"]["deployment"])
        self.assertFalse(checks["report_has_substantive_closing_prose"])

        negated_id = baseline.replace(
            f"deployment value is {values['deployment']}",
            f"deployment value is not {values['deployment']}", 1)
        checks = replay._first_report_checks(negated_id, tools, self.fixture)
        self.assertFalse(checks["synthetic_value_checks"]["deployment"])

        reversed_states = baseline.replace(
            "the follow-through file was absent", "the follow-through file was present", 1).replace(
            "the file was present and", "the file was absent and", 1)
        self.assertNotEqual(reversed_states, baseline)
        checks = replay._first_report_checks(reversed_states, tools, self.fixture)
        self.assertFalse(checks["prose_content_checks"]["describes_before_state_and_zero_actions"])

        negated_verification = baseline.replace(
            "passed all six checks", "did not pass all six checks", 1)
        checks = replay._first_report_checks(negated_verification, tools, self.fixture)
        self.assertFalse(checks["prose_content_checks"]["reports_six_check_verification_result"])

    def test_content_mutations_fail_while_valid_json_and_tool_evidence_stay_intact(self):
        tools = self._valid_tool_trace()
        values = self.fixture["facts"]["report"]["synthetic_values"]
        ids = {key: replay._expected_identifier(key, value) for key, value in values.items()}
        text = self._report_text().split("\nFACTUAL_REPORT_JSON:\n", 1)[0]
        ko = self._report_text(language="ko").split("\nFACTUAL_REPORT_JSON:\n", 1)[0]
        swap = text.replace(ids["commit"], "@@").replace(ids["deployment"], ids["commit"]).replace(
            "@@", ids["deployment"])
        cases = (
            ("commit missing", text.replace(ids["commit"], "unknown"), "names_actual_synthetic_values"),
            ("commit longer fake", text.replace(ids["commit"], ids["commit"] + "x"),
             "names_actual_synthetic_values"),
            ("roles swapped", swap, "names_actual_synthetic_values"),
            ("serving negated", text.replace("serving value is", "serving value is not"),
             "names_actual_synthetic_values"),
            ("ko deployment wrong", ko.replace(ids["deployment"], ids["deployment"] + "9"),
             "names_actual_synthetic_values"),
            ("reason missing", text.replace("I completed the already approved isolated parent "
                                            "follow-through after reading the exact result. ", ""),
             "explains_fixture_reason"),
            ("before/after missing", text.replace("Before the action", "Earlier").replace(
                "after the action", "later").replace("the follow-through file was absent and", "x"),
             "describes_before_state_and_zero_actions"),
            ("count reversed", text.replace("count was 0", "count was 1", 1).replace(
                "count was 1.", "count was 0.", 1), "describes_before_state_and_zero_actions"),
            ("verification failed", text.replace("passed all six checks with 0 failed",
                                                 "failed 2 of six checks"),
             "reports_six_check_verification_result"),
            ("skip missing", text.replace("Production deployment and serving checks were skipped. ", ""),
             "states_fixture_skip_and_operating_limit"),
            ("production changed", text.replace("was changed or measured", "was changed and measured")
             .replace("No production endpoint", "A production endpoint"),
             "states_fixture_skip_and_operating_limit"),
            ("scope limit missing", text.replace("This isolated Codex replay tests delivery and "
                                                 "reporting only.", ""),
             "states_isolated_replay_scope_limit"),
        )
        marker = "\nFACTUAL_REPORT_JSON:\n"
        json_tail = marker + json.dumps(self.fixture["facts"]["report"], ensure_ascii=False)
        for name, mutated, key in cases:
            with self.subTest(name):
                self.assertNotEqual(mutated, text if "ko" not in name else ko)
                mutated = mutated + json_tail
                checks = replay._first_report_checks(mutated, tools, self.fixture)
                self.assertTrue(checks["report_matches_fixture_facts"])
                self.assertTrue(checks["every_exact_artifact_was_read"])
                self.assertFalse(checks["report_has_substantive_closing_prose"])
                failed = [k for k, v in {**checks["prose_content_checks"],
                                         **{f"id_{k}": v for k, v in
                                            checks["synthetic_value_checks"].items()}}.items() if not v]
                self.assertTrue(failed, name)

    def test_keyword_presence_cannot_substitute_for_factual_values_or_action_trace(self):
        tools = self._valid_tool_trace()
        facts = self.fixture["facts"]
        wrong = dict(facts["report"])
        wrong["synthetic_values"] = dict(wrong["synthetic_values"], deployment="production deployment succeeded")
        text = ("The fixture deployment passed verification and the synthetic service is serving.\n"
                "FACTUAL_REPORT_JSON:\n" + json.dumps(wrong))
        checks = replay._first_report_checks(text, tools, self.fixture)
        self.assertFalse(checks["report_matches_fixture_facts"])

        action = next(item for item in tools if item["id"] == "action-1")
        duplicate = {**action, "id": "action-duplicate"}
        checks = replay._first_report_checks(self._report_text(), [*tools, duplicate], self.fixture)
        self.assertFalse(checks["one_approved_followthrough_tool_invocation"])

        missing_read = list(tools)
        read_item = dict(next(item for item in tools if item["id"] == "read-1"))
        missing_path = self.fixture["artifact_paths"][0]
        remaining = [path for path in [self.fixture["facts_path"], *self.fixture["artifact_paths"]]
                     if path != missing_path]
        relative = [path.relative_to(self.worktree) for path in remaining]
        read_command = "cat " + " ".join(path.as_posix() for path in relative)
        read_item["commandActions"] = [{"command": read_command, "type": "unknown"}]
        missing_read[missing_read.index(next(item for item in missing_read if item["id"] == "read-1"))] = read_item
        checks = replay._first_report_checks(self._report_text(), missing_read, self.fixture)
        self.assertFalse(checks["every_exact_artifact_was_read"])

        disconnected = ("Approved. Before after. Checks pass. Synthetic commit deployment serving. "
                        "Production skipped not measured.\nFACTUAL_REPORT_JSON:\n"
                        + json.dumps(facts["report"], ensure_ascii=False))
        checks = replay._first_report_checks(disconnected, tools, self.fixture)
        self.assertFalse(checks["report_has_substantive_closing_prose"])
        self.assertFalse(checks["prose_content_checks"]["explains_fixture_reason"])
        self.assertFalse(checks["prose_content_checks"]["describes_before_state_and_zero_actions"])
        self.assertFalse(checks["prose_content_checks"]["reports_six_check_verification_result"])
        self.assertFalse(checks["synthetic_value_checks"]["commit"])

        json_only = "FACTUAL_REPORT_JSON:\n" + json.dumps(facts["report"], ensure_ascii=False)
        checks = replay._first_report_checks(json_only, tools, self.fixture)
        self.assertFalse(checks["report_has_substantive_closing_prose"])

    def test_natural_korean_prose_is_bound_to_the_fixture_values(self):
        checks = replay._first_report_checks(self._report_text(language="ko"),
                                             self._valid_tool_trace(), self.fixture)
        self.assertTrue(checks["report_matches_fixture_facts"])
        self.assertTrue(checks["report_has_substantive_closing_prose"])
        self.assertTrue(all(checks["prose_content_checks"].values()))
        self.assertTrue(all(checks["synthetic_value_checks"].values()))

    def test_natural_multisentence_prose_is_independent_of_exact_json(self):
        facts = self.fixture["facts"]["report"]
        values = facts["synthetic_values"]
        prose = (
            "I read the exact result. I completed the already approved follow-through after reading it "
            "so the requested task could be fulfilled.\n"
            "Before, the follow-through file was absent and the approved action count was 0. "
            "Afterward, the file was present and the approved action count was 1.\n"
            "Verification with verify_fixture.py passed all six checks; 0 failed.\n"
            f"The synthetic commit value is {values['commit']}. "
            f"The synthetic deployment value is {values['deployment']}. "
            f"The synthetic serving value is {values['serving']}.\n"
            "Production deployment and serving checks were skipped. "
            "No production operating state was changed or measured. "
            "This isolated Codex replay tests delivery and reporting only."
        )
        tools = self._valid_tool_trace()
        without_json = replay._first_report_checks(prose, tools, self.fixture)
        with_json = replay._first_report_checks(
            prose + "\nFACTUAL_REPORT_JSON:\n" + json.dumps(facts, ensure_ascii=False),
            tools, self.fixture)
        self.assertTrue(without_json["report_has_substantive_closing_prose"])
        self.assertTrue(with_json["report_has_substantive_closing_prose"])
        self.assertFalse(without_json["report_matches_fixture_facts"])
        self.assertTrue(with_json["report_matches_fixture_facts"])
        self.assertEqual(without_json["prose_content_checks"], with_json["prose_content_checks"])

        wrong_value = prose.replace(values["deployment"], "wrong-deployment-value")
        wrong = replay._first_report_checks(
            wrong_value + "\nFACTUAL_REPORT_JSON:\n" + json.dumps(facts, ensure_ascii=False),
            tools, self.fixture)
        self.assertFalse(wrong["report_has_substantive_closing_prose"])
        self.assertFalse(wrong["synthetic_value_checks"]["deployment"])

    def test_compound_shell_results_do_not_prove_verifier_or_action_success(self):
        tools = self._valid_tool_trace()
        verifier = next(item for item in tools if item["id"] == "verify-1")
        exact_verifier = dict(verifier)
        verifier_command = ("python3 -B .agent_reports/native-parent-fixture/verify_fixture.py; "
                            "echo FIXTURE_VERIFICATION=PASS checks=6 failed=0")
        verifier["command"] = "/usr/bin/zsh -lc " + json.dumps(verifier_command)
        verifier["commandActions"] = [{"command": verifier_command, "type": "unknown"}]
        verifier["aggregatedOutput"] = "FIXTURE_VERIFICATION=PASS checks=6 failed=0"
        checks = replay._first_report_checks(self._report_text(), tools, self.fixture)
        self.assertFalse(checks["fixture_verifier_passed_in_native_tool_trace"])
        self.assertIn("ambiguous-shell-compound-no-per-command-result",
                      checks["verifier_invocations"][0]["execution_result_reason"])

        mismatched = {**exact_verifier,
                      "command": "/usr/bin/zsh -lc 'echo FIXTURE_VERIFICATION=PASS checks=6 failed=0'"}
        checks = replay._first_report_checks(
            self._report_text(), [tools[0], tools[1], mismatched], self.fixture)
        self.assertFalse(checks["fixture_verifier_passed_in_native_tool_trace"])
        self.assertEqual(checks["verifier_invocations"][0]["execution_result_reason"],
                         "command-action-does-not-match-execution-envelope")

        absolute_command = f"python3 -B {self.fixture['verifier']}"
        wrong_action_cwd = {
            **exact_verifier,
            "command": "/usr/bin/zsh -lc " + json.dumps(absolute_command),
            "commandActions": [{"command": absolute_command, "cwd": str(self.root),
                                "type": "unknown"}],
        }
        checks = replay._first_report_checks(
            self._report_text(), [tools[0], tools[1], wrong_action_cwd], self.fixture)
        self.assertFalse(checks["fixture_verifier_passed_in_native_tool_trace"])
        self.assertEqual(checks["verifier_invocations"][0]["execution_result_reason"],
                         "command-action-cwd-does-not-match-execution-envelope")

        action = next(item for item in tools if item["id"] == "action-1")
        action_command = (f"python3 -B .agent_reports/native-parent-fixture/"
                          f"apply_fixture_followthrough.py {self.sentinel} || true")
        action["command"] = "/usr/bin/zsh -lc " + json.dumps(action_command)
        action["commandActions"] = [{"command": action_command, "type": "unknown"}]
        checks = replay._first_report_checks(self._report_text(), tools, self.fixture)
        self.assertFalse(checks["one_approved_followthrough_tool_invocation"])

    def test_verifier_marker_and_success_must_belong_to_the_same_invocation(self):
        tools = self._valid_tool_trace()
        verifier = next(item for item in tools if item["id"] == "verify-1")
        marker = "FIXTURE_VERIFICATION=PASS checks=6 failed=0"
        failed_with_marker = {
            **verifier, "id": "verify-failed-with-marker", "status": "failed",
            "exitCode": 1, "aggregatedOutput": marker,
        }
        successful_without_marker = {
            **verifier, "id": "verify-success-without-marker", "status": "completed",
            "exitCode": 0, "aggregatedOutput": "unrelated output",
        }
        checks = replay._first_report_checks(
            self._report_text(), [*tools[:2], failed_with_marker, successful_without_marker],
            self.fixture)
        self.assertFalse(checks["fixture_verifier_passed_in_native_tool_trace"])
        self.assertEqual(len(checks["verifier_invocations"]), 2)
        self.assertTrue(checks["verifier_invocations"][0]["output_marker_observed"])
        self.assertFalse(checks["verifier_invocations"][0]["tool_succeeded"])
        self.assertFalse(checks["verifier_invocations"][1]["output_marker_observed"])
        self.assertTrue(checks["verifier_invocations"][1]["tool_succeeded"])

    def test_compound_shell_uses_only_matching_per_action_result_and_output(self):
        tools = self._valid_tool_trace()
        verifier = next(item for item in tools if item["id"] == "verify-1")
        script = ".agent_reports/native-parent-fixture/verify_fixture.py"
        command = f"python3 -B {script}; echo unrelated"
        marker = "FIXTURE_VERIFICATION=PASS checks=6 failed=0"
        compound = {
            **verifier, "command": "/usr/bin/zsh -lc " + json.dumps(command),
            "commandActions": [
                {"command": f"python3 -B {script}", "cwd": str(self.worktree),
                 "type": "unknown", "status": "completed", "exitCode": 0,
                 "aggregatedOutput": marker},
                {"command": "echo unrelated", "cwd": str(self.worktree),
                 "type": "unknown", "status": "completed", "exitCode": 0,
                 "aggregatedOutput": "unrelated"},
            ],
            "aggregatedOutput": marker + "\nunrelated",
            "status": "completed", "exitCode": 0,
        }
        checks = replay._first_report_checks(
            self._report_text(), [*tools[:2], compound], self.fixture)
        self.assertTrue(checks["fixture_verifier_passed_in_native_tool_trace"])
        self.assertTrue(checks["verifier_invocations"][0]["execution_result_proven"])

        mixed = {
            **compound,
            "commandActions": [
                {**compound["commandActions"][0], "status": "failed", "exitCode": 1},
                {**compound["commandActions"][0], "status": "completed", "exitCode": 0,
                 "aggregatedOutput": "no marker"},
            ],
        }
        mixed["command"] = "/usr/bin/zsh -lc " + json.dumps(f"python3 -B {script}; python3 -B {script}")
        checks = replay._first_report_checks(
            self._report_text(), [*tools[:2], mixed], self.fixture)
        self.assertFalse(checks["fixture_verifier_passed_in_native_tool_trace"])
        self.assertEqual(len(checks["verifier_invocations"]), 2)

    def test_duplicate_identical_action_entries_are_counted_separately(self):
        tools = self._valid_tool_trace()
        action = next(item for item in tools if item["id"] == "action-1")
        action["commandActions"] = [dict(action["commandActions"][0]),
                                    dict(action["commandActions"][0])]
        checks = replay._first_report_checks(self._report_text(), tools, self.fixture)
        self.assertEqual(len(checks["followthrough_invocations"]), 2)
        self.assertFalse(checks["one_approved_followthrough_tool_invocation"])

    def test_fixture_followthrough_script_rejects_a_second_action(self):
        first = subprocess.run([sys.executable, "-B", str(self.fixture["action"]), self.sentinel],
                               capture_output=True, text=True, check=False)
        second = subprocess.run([sys.executable, "-B", str(self.fixture["action"]), self.sentinel],
                                capture_output=True, text=True, check=False)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertNotEqual(second.returncode, 0)
        state = json.loads(self.fixture["followthrough"].read_text(encoding="utf-8"))
        self.assertEqual(state["approved_action_count"], 1)
        self.assertEqual(state["sentinel"], self.sentinel)

    def test_old_ack_cleanup_repeat_and_new_conflict_revision_remain_distinct(self):
        import dispatch_pending_delivery as pending
        import dispatch_supervision as supervision

        jobs = self.root / "jobs.log"
        attempt, route_id, receipt, completion, metadata = replay._fixture_registry(
            jobs, "idle-parent", self.worktree, self.sentinel)
        pending.claim(self.root, "idle-parent", completion["delivery_id"],
                      claim_owner="fixture-consumer", lease_seconds=30,
                      require_generation_proof=False, jobs=jobs)
        acked = pending.ack(self.root, "idle-parent", completion["delivery_id"],
                            acked_by="codex-native-prompt:idle-parent")
        old_ack = pending.received_ack_identity(acked, "idle-parent", completion["delivery_id"])

        cleanup = supervision.materialize(jobs, {attempt}, reason="join-deadline")[0]
        self.assertFalse(supervision.notice_is_current(cleanup))
        self.assertIn("retains cleanup observation only",
                      supervision.render_text(cleanup["receipt"]))
        spec = importlib.util.spec_from_file_location(
            "codex_parent_replay_courier_test", replay.UTILITIES / "codex-managed-completion.py")
        courier = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = courier
        spec.loader.exec_module(courier)
        queue_socket, native_history_path, native_rollout = self._native_queue_fixture("idle-parent")
        watcher_args = SimpleNamespace(
            jobs=jobs.resolve(), parent_session_id="idle-parent",
            sealed_batch_id=metadata["managed_sealed_batch_id"],
            interval=0.05, queue_socket=queue_socket,
        )
        watcher = courier.NoticeWatcher(watcher_args, {attempt}, capability=None)
        cleanup_path = pending.record_path(self.root, "idle-parent", cleanup["delivery_id"])
        with mock.patch.object(courier.codex_queue_delivery, "send_at_least_once") as cleanup_send, \
             mock.patch.object(courier.codex_queue_delivery, "list_queue") as cleanup_list, \
             mock.patch.object(courier.codex_queue_delivery, "find_turn_by_client_message_id") as cleanup_find:
            watcher._one(cleanup_path)
        cleanup_send.assert_not_called()
        cleanup_list.assert_not_called()
        cleanup_find.assert_not_called()
        rejected_cleanup = pending.read(self.root, "idle-parent", cleanup["delivery_id"])
        self.assertEqual(rejected_cleanup["state"], "rejected")
        self.assertEqual(rejected_cleanup["expiry_reason"], "supervision-resolved")
        old_after_cleanup = pending.read(self.root, "idle-parent", completion["delivery_id"])
        self.assertEqual(pending.received_ack_identity(
            old_after_cleanup, "idle-parent", completion["delivery_id"]), old_ack)
        duplicate = pending.create(
            self.root, recipient_kind="codex-native-queue", recipient_key="idle-parent",
            delivery_id=completion["delivery_id"], session_generation="",
            session_generation_supported="0", attempt_ids=[attempt], parent_attempt_id="-",
            route_id=route_id, route_node="_owner", receipt=receipt,
            receipt_digest=completion["receipt_digest"],
            row_revisions={attempt: metadata["delivery_row_revision"]})
        self.assertEqual(duplicate["state"], "acked")
        self.assertEqual(pending.received_ack_identity(
            pending.read(self.root, "idle-parent", completion["delivery_id"]),
            "idle-parent", completion["delivery_id"]), old_ack)

        conflict_metadata = {
            **metadata, "cleanup_state": "unknown", "terminal_conflict": "1",
            "conflicting_terminal_note": "synthetic-late-failure",
            "conflicting_failure_class": "fail",
            "conflicting_classifier_source": "native-replay-late-fixture",
        }
        jobs.write_text(
            "2026-10-19T00:00:00Z\tdone\t" + str(self.worktree) + "\t" + str(self.worktree)
            + "\tisolated-parent-replay\t"
            + ",".join(f"{key}={value}" for key, value in conflict_metadata.items()) + "\n",
            encoding="utf-8")
        late = supervision.materialize(jobs, {attempt}, reason="terminal-evidence-conflict")[0]
        self.assertNotEqual(late["delivery_id"], cleanup["delivery_id"])
        self.assertNotEqual(late["delivery_id"], completion["delivery_id"])
        self.assertTrue(late["receipt"]["obligation_revision"])
        self.assertTrue(supervision.notice_is_current(late))
        self.assertNotEqual(late["delivery_id"], cleanup["delivery_id"])
        self.assertNotEqual(late["delivery_id"], completion["delivery_id"])
        self.assertEqual(pending.read(self.root, "idle-parent", cleanup["delivery_id"])["expiry_reason"],
                         "supervision-resolved")
        late_path = pending.record_path(self.root, "idle-parent", late["delivery_id"])
        queue = courier.codex_queue_delivery
        notice_context = courier.notice_receipt.context(
            late["receipt"], courier.notice_receipt.gateway_delivery_id(late["receipt"]))
        additional = notice_context.get("additionalContext")
        notice_values = [value.get("value") for value in additional.values()
                         if isinstance(value, dict) and isinstance(value.get("value"), str)] \
            if isinstance(additional, dict) else []
        self.assertEqual(len(notice_values), 1)
        from dispatch_recipient_receipt import receipt_marker_for_identity
        # The native queue proves generation 1 at the claim boundary. The
        # marker binds that original claimed identity, not the pre-claim row.
        expected_claim = {**late, "session_generation": "1",
                          "session_generation_supported": "1"}
        notice_identity = pending.received_ack_identity(
            expected_claim, "idle-parent", late["delivery_id"])
        notice_text = notice_values[0] + "\n" + receipt_marker_for_identity(
            late["delivery_id"], notice_identity)
        self.assertEqual(watcher._native_notice_text(expected_claim, "idle-parent"),
                         (notice_text, notice_values[0]))
        native_turn_id = "isolated-native-turn-1"
        native_requests = []
        prepared_fences = []
        observation_statuses = []
        observed_turns = []
        events = []
        expected_native_item = {
            "type": "userMessage", "clientId": late["delivery_id"],
            "content": [{"type": "text", "text": notice_text, "text_elements": []}],
        }

        def fake_rpc(path, method, params, *, timeout, session=None):
            native_requests.append((method, dict(params)))
            if method == "thread/queue/list":
                return {"data": []}
            if method == "thread/turns/list":
                return {"data": [{"id": "isolated-prior-turn", "status": "completed"}]}
            if method != "thread/queue/add":
                self.fail(f"unexpected native queue RPC: {method}")

            record = pending.read(self.root, "idle-parent", late["delivery_id"])
            identity = pending.submission_fence_identity(record, params.get("threadId"))
            fence = pending.read_submission_fence(
                self.root, "idle-parent", late["delivery_id"], expected_identity=identity)
            prepared_fences.append(fence.get("state") if fence else None)

            submitted_input = params.get("input")
            native_content = [
                {**item, "text_elements": item.get("text_elements", [])}
                for item in submitted_input if isinstance(item, dict)
            ] if isinstance(submitted_input, list) else []
            native_item = {
                "type": "userMessage", "clientId": params.get("clientUserMessageId"),
                "content": native_content,
            }
            history = sqlite3.connect(native_history_path)
            history.execute("INSERT INTO thread_turns VALUES (?, ?, 'completed')",
                            (params.get("threadId"), native_turn_id))
            history.execute(
                "INSERT INTO thread_items VALUES (?, ?, ?, ?, 'userMessage', 1)",
                (params.get("threadId"), native_turn_id, "isolated-native-item-1",
                 json.dumps(native_item, ensure_ascii=False)),
            )
            history.execute(
                "UPDATE thread_history_projection_state "
                "SET next_rollout_byte_offset = ?, next_rollout_ordinal = 2 WHERE thread_id = ?",
                (native_rollout.stat().st_size, params.get("threadId")),
            )
            history.commit()
            history.close()
            events.append("queue-add-accepted")
            return {"queuedSubmission": {"id": native_turn_id}}

        real_observe = queue._observe_peer_message

        def observe_native_history(*args, **kwargs):
            observation = real_observe(*args, **kwargs)
            observation_statuses.append(observation.status)
            if observation.status == "matched":
                observed_turns.append(observation.turn)
                events.append("exact-consumption-observed")
            return observation

        real_send = queue.send_at_least_once
        real_claim = pending.claim
        real_ack = pending.ack_received

        def ack_after_exact_consumption(*args, **kwargs):
            self.assertIn("exact-consumption-observed", events)
            self.assertEqual(args, (self.root, "idle-parent", late["delivery_id"]))
            self.assertEqual(kwargs["expected_identity"], notice_identity)
            events.append("acked")
            return real_ack(*args, **kwargs)

        with ExitStack() as stack:
            late_send = stack.enter_context(mock.patch.object(
                queue, "send_at_least_once", wraps=real_send))
            native_rpc = stack.enter_context(mock.patch.object(
                queue, "_rpc", side_effect=fake_rpc))
            native_observe = stack.enter_context(mock.patch.object(
                queue, "_observe_peer_message", side_effect=observe_native_history))
            late_claim = stack.enter_context(mock.patch.object(
                pending, "claim", wraps=real_claim))
            late_ack = stack.enter_context(mock.patch.object(
                pending, "ack_received", side_effect=ack_after_exact_consumption))
            watcher._one(late_path)

        self.assertFalse(watcher.errors, list(watcher.errors))
        late_send.assert_called_once()
        late_claim.assert_called_once()
        self.assertTrue(late_claim.call_args.kwargs["require_generation_proof"])
        self.assertEqual(late_claim.call_args.kwargs["live_recipient_generation"],
                         ("idle-parent", "1"))
        self.assertEqual(late_send.call_args.kwargs["message"], notice_text)
        self.assertTrue(late_send.call_args.kwargs["require_message_match"])
        self.assertTrue(callable(late_send.call_args.kwargs["before_add"]))
        self.assertEqual([method for method, _params in native_requests], [
            "thread/queue/list", "thread/turns/list", "thread/queue/add", "thread/queue/list",
        ])
        add_params = next(params for method, params in native_requests if method == "thread/queue/add")
        self.assertEqual(add_params, {
            "threadId": "idle-parent", "clientUserMessageId": late["delivery_id"],
            "input": [{"type": "text", "text": notice_text}],
        })
        self.assertEqual(prepared_fences, ["prepared"])
        self.assertEqual(observation_statuses, ["not_observed", "not_observed", "matched"])
        self.assertEqual(native_observe.call_count, 3)
        self.assertEqual([turn["id"] for turn in observed_turns], [native_turn_id])
        self.assertEqual(observed_turns[0]["items"], [expected_native_item])
        self.assertEqual(events, ["queue-add-accepted", "exact-consumption-observed", "acked"])
        late_ack.assert_called_once()
        late_record = pending.read(self.root, "idle-parent", late["delivery_id"])
        self.assertEqual(late_record["state"], "acked")
        self.assertEqual(late_record["acked_by"], watcher.owner)
        self.assertEqual(pending.received_ack_identity(
            late_record, "idle-parent", late["delivery_id"]), notice_identity)
        fence_identity = pending.submission_fence_identity(late_record, "idle-parent")
        accepted_fence = pending.read_submission_fence(
            self.root, "idle-parent", late["delivery_id"], expected_identity=fence_identity)
        self.assertEqual(accepted_fence["state"], "accepted")
        self.assertEqual(accepted_fence["queue_submission_id"], native_turn_id)
        self.assertEqual(accepted_fence["record_identity"], notice_identity)
        old_after_conflict = pending.read(self.root, "idle-parent", completion["delivery_id"])
        self.assertEqual(pending.received_ack_identity(
            old_after_conflict, "idle-parent", completion["delivery_id"]), old_ack)

        # Check repeat observation separately from the exact first-send trace.
        with mock.patch.object(queue, "send_at_least_once") as repeat_send, \
             mock.patch.object(queue, "_rpc") as repeat_rpc:
            watcher._one(late_path)
        repeat_send.assert_not_called()
        repeat_rpc.assert_not_called()
        self.assertFalse(watcher.errors, list(watcher.errors))
        self.assertEqual(pending.received_ack_identity(
            pending.read(self.root, "idle-parent", late["delivery_id"]),
            "idle-parent", late["delivery_id"]), notice_identity)

    def test_resolver_rejects_symlink_into_group_or_world_writable_directory(self):
        with tempfile.TemporaryDirectory(prefix="parent-replay-unsafe-endpoint-") as temporary:
            root = Path(temporary)
            target_dir = root / "unsafe"
            target_dir.mkdir(mode=0o700)
            target_dir.chmod(0o777)
            target = target_dir / "server.sock"
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            listener.bind(str(target))
            endpoint = root / "endpoint.sock"
            endpoint.symlink_to(target)
            try:
                with self.assertRaisesRegex(
                        replay.codex_queue_delivery.QueueDeliveryError,
                        "queue-endpoint-path-unsafe"):
                    replay.codex_queue_delivery.resolve_endpoint(endpoint)
            finally:
                listener.close()


if __name__ == "__main__":
    unittest.main()
