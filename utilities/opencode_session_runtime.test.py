#!/usr/bin/env python3
"""Native process/session boundaries and the shared controller, without model calls."""
import importlib.util
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import time
import selectors
from types import SimpleNamespace
import unittest
import unittest.mock
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "utilities"))
import opencode_session_runtime as runtime
import dispatch_parent_completion as parent
import opencode_runtime as permission_runtime
import dispatch_replacement
from dispatch_contract import hold_supervisor_lease

spec = importlib.util.spec_from_file_location(
    "shared_controller_fixture", ROOT / "utilities/claude_session_supervisor.test.py")
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)


class NativeSessionTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        clean = {k: v for k, v in os.environ.items()
                 if not k.startswith(("AGENT_", "HERDR_", "OPENCODE_", "CLAUDE_", "CODEX_", "XDG_"))}
        clean.update(HOME=str(self.root / "home"), XDG_STATE_HOME=str(self.root / "state"),
                     XDG_CONFIG_HOME=str(self.root / "config"), XDG_DATA_HOME=str(self.root / "data"),
                     XDG_CACHE_HOME=str(self.root / "cache"), OPENCODE_RUNTIME_VERSION="1.19.0")
        environment = unittest.mock.patch.dict(os.environ, clean, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        self.script = self.root / "native.py"
        self.args = SimpleNamespace(
            worktree=str(self.root), jobs=str(self.root / "jobs.log"),
            parent_attempt_id="att-native", turn_timeout=2,
            opencode_command=shlex.join([sys.executable, str(self.script)]),
            model="opencode-go/glm-5.3-flash", variant="runtime-default", opencode_agent="build")
        self.events = []

    def program(self, body):
        self.script.write_text("import json, sys, time\n"
            "prompt = sys.stdin.read()\n"
            "def event(kind, **values):\n"
            " print(json.dumps(dict(type=kind, sessionID='ses_native', **values)), flush=True)\n"
            + body)

    def test_v2_command_uses_cwd_and_provider_model_variant_syntax(self):
        with unittest.mock.patch.object(runtime.opencode_runtime, "is_v2", return_value=True):
            argv = runtime.command(self.args, "ses_native")
        self.assertNotIn("--standalone", argv)
        self.assertIn("--session", argv)
        self.assertIn("ses_native", argv)
        self.assertEqual(argv[argv.index("--model") + 1], "opencode-go/glm-5.3-flash")
        self.assertNotIn("--dir", argv)
        self.assertNotIn("--variant", argv)
        self.args.variant = "high"
        with unittest.mock.patch.object(runtime.opencode_runtime, "is_v2", return_value=True):
            argv = runtime.command(self.args, "")
        self.assertEqual(argv[argv.index("--model") + 1], "opencode-go/glm-5.3-flash#high")
        self.args.opencode_standalone = True
        with unittest.mock.patch.object(runtime.opencode_runtime, "is_v2", return_value=True):
            argv = runtime.command(self.args, "")
        self.assertIn("--standalone", argv)
        self.assertEqual(argv[argv.index("--model") + 1], "opencode-go/glm-5.3-flash#high")

    def test_supervisor_accepts_the_internal_standalone_worker_mode(self):
        args = fixture.supervisor.parser().parse_args([
            "--runtime-harness", "opencode", "--opencode-standalone",
            "--worktree", str(self.root), "--jobs", str(self.root / "jobs.log"),
            "--parent-attempt-id", "att-native",
        ])
        self.assertTrue(args.opencode_standalone)

    def test_v2_stream_without_terminal_event_uses_native_wait_and_exact_session(self):
        self.program("event('step_start', part={})\n"
                     "event('text', part={'text':'answer'})\n")
        with unittest.mock.patch.object(runtime.opencode_runtime, "is_v2", return_value=True):
            result, code = runtime.run_turn(self.args, "question", emit=self.events.append)
        self.assertEqual(code, 0)
        self.assertEqual(result["session_id"], "ses_native")
        self.assertEqual(result["result"], "answer")
        self.assertEqual(result["subtype"], "success")

    def test_v2_identityless_provider_rejection_is_preserved_without_secret(self):
        self.program("print(json.dumps({'type':'error','sessionID':'','error':{"
                     "'type':'provider.auth','code':'api_key_expired','status':401,"
                     "'message':'api_key_expired Bearer secret-token'}}), flush=True)\n"
                     "sys.exit(1)\n")
        with unittest.mock.patch.object(runtime.opencode_runtime, "is_v2", return_value=True):
            result, code = runtime.run_turn(self.args, "question", emit=self.events.append)
        self.assertEqual(code, 1)
        self.assertTrue(result["is_error"])
        self.assertEqual(result["error"]["code"], "api_key_expired")
        self.assertNotIn("secret-token", json.dumps(result))

    def test_v2_typed_auth_rejection_is_recorded_and_stays_a_replaceable_runtime_death(self):
        # The exact stream of the replacement owner that died on 2026-10-20:
        # OpenCode 2.0.32 types the provider failure and carries no status.
        import sqlite3
        import dispatch_supervisor_terminal as terminal
        incident = {"type": "provider.auth", "message": "Request failed: 401"}
        db = self.root / "opencode.db"
        with sqlite3.connect(db) as con:
            con.execute("CREATE TABLE credential (id TEXT PRIMARY KEY, integration_id TEXT, "
                        "label TEXT, value TEXT, active INTEGER, time_created INTEGER, "
                        "time_updated INTEGER)")
            con.execute("INSERT INTO credential VALUES ('oai', 'openai', 'Default', ?, 1, 1, 1)",
                        (json.dumps({"type": "oauth", "access": "openai-access",
                                     "refresh": "openai-refresh"}),))
        native = self.root / "bin" / "opencode"  # only a native binary records outcomes
        native.parent.mkdir()
        native.write_text("#!" + sys.executable + "\nimport json, sys\nsys.stdin.read()\n"
                          "print(json.dumps({'type':'error','sessionID':'','error':%r}), flush=True)\n"
                          "sys.exit(1)\n" % incident)
        native.chmod(0o755)
        self.args.opencode_command = str(native)
        self.args.model = "openai/gpt-6.1-sol"
        with unittest.mock.patch.dict(os.environ, {"OPENCODE_RUNTIME_VERSION": "2.0.32",
                                                   "OPENCODE_DB": str(db)}):
            result, code = runtime.run_turn(self.args, "question", emit=self.events.append)
            evidence = runtime.opencode_runtime.model_availability("openai/gpt-6.1-sol")
        self.assertEqual(code, 1)
        self.assertEqual(result["error"], incident)  # no status is synthesized
        cache = json.loads((self.root / "state/agent-fleet/usage/opencode.json").read_text())
        recorded = {row["model"]: (row["state"], row["reason"])
                    for row in cache["providers"].values() if row["provider"] == "openai"}
        self.assertEqual(recorded, {"gpt-6.1-sol": ("unavailable", "authentication-rejected"),
                                    "*": ("unavailable", "authentication-rejected")})
        self.assertNotIn("openai-access", json.dumps(cache))
        self.assertEqual((evidence["state"], evidence["automatic_eligible"]),
                         ("unavailable", False))
        verdict = terminal.classify_session_result(result, code, runtime="opencode")
        self.assertEqual((verdict.note, verdict.failure_class, verdict.api_status),
                         ("dead-runtime-error", "runtime", ""))

    def test_exact_session_resume_and_only_final_step_is_returned(self):
        self.program("""
session = sys.argv[sys.argv.index('--session') + 1] if '--session' in sys.argv else None
with open('trace', 'a') as f: f.write(json.dumps([session, prompt]) + '\\n')
event('step_start', part={})
event('text', part={'text': 'intermediate deliberation'})
event('tool_use', part={'tool':'bash', 'callID':'call-exact', 'state':{'status':'completed', 'output':'private'}})
event('step_finish', part={'reason':'tool-calls'})
event('step_start', part={})
event('text', part={'text':'artifact: -\\nverdict: PASS\\nblocker: none'})
event('step_finish', part={'reason':'stop'})
""")
        first, code = runtime.run_turn(self.args, "first", emit=self.events.append)
        second, code2 = runtime.run_turn(self.args, "second", emit=self.events.append)
        self.assertEqual((code, code2), (0, 0))
        self.assertEqual(first, second)
        self.assertEqual(first['result'], 'artifact: -\nverdict: PASS\nblocker: none')
        self.assertEqual([json.loads(x) for x in (self.root/'trace').read_text().splitlines()],
                         [[None, 'first'], ['ses_native', 'second']])
        self.assertEqual([e['type'] for e in self.events],
                         ['dispatch.supervisor.session', 'tool_use', 'dispatch.supervisor.session', 'tool_use'])
        self.assertNotIn('private', json.dumps(self.events))

    def test_direct_and_resumed_native_processes_inherit_the_final_config_override(self):
        self.program("""
import os
session = sys.argv[sys.argv.index('--session') + 1] if '--session' in sys.argv else None
with open('config-trace', 'a') as f:
 f.write(json.dumps({'session':session, 'config':os.environ.get('OPENCODE_CONFIG_CONTENT')}) + '\\n')
event('step_start', part={})
event('text', part={'text':'artifact: -\\nverdict: PASS\\nblocker: none'})
event('step_finish', part={'reason':'stop'})
""")
        native_config = json.dumps({"provider": {"opencode-go": {"models": {
            "glm-5.3-flash": {"limit": {"context": 600000, "input": 480000, "output": 8192}}
        }}}}, separators=(",", ":"))
        with unittest.mock.patch.dict(os.environ, {"OPENCODE_CONFIG_CONTENT": native_config,
                                                    "XDG_CONFIG_HOME": str(self.root / "masked-config")}):
            runtime.run_turn(self.args, "first", emit=self.events.append)
            runtime.run_turn(self.args, "resume", emit=self.events.append)
        rows = [json.loads(line) for line in (self.root / "config-trace").read_text().splitlines()]
        self.assertEqual([row["session"] for row in rows], [None, "ses_native"])
        self.assertEqual([row["config"] for row in rows], [native_config, native_config])

    def test_session_switch_is_rejected_and_original_binding_preserved(self):
        runtime.bind_session(self.args, 'ses_original')
        self.program("event('step_start', part={})\n")
        with self.assertRaisesRegex(runtime.OpenCodeTransportError, 'identity-mismatch'):
            runtime.run_turn(self.args, 'continue', emit=self.events.append)
        self.assertEqual(runtime.read_binding(self.args), 'ses_original')

    def test_foreign_or_dangling_native_binding_is_not_adopted(self):
        runtime.bind_session(self.args, 'ses_original')
        path = runtime.binding_path(self.args)
        value = json.loads(path.read_text())
        value['attempt_id'] = 'att-other'
        path.write_text(json.dumps(value))
        with self.assertRaisesRegex(runtime.OpenCodeTransportError, 'binding-invalid'):
            runtime.read_binding(self.args)
        path.unlink()
        path.symlink_to(self.root/'missing')
        with self.assertRaisesRegex(runtime.OpenCodeTransportError, 'binding-unsafe'):
            runtime.read_binding(self.args)

    def test_bound_owner_turn_config_reprojects_and_keeps_read_root(self):
        import commit_policy
        import subprocess

        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        git_dir = Path(subprocess.check_output(
            ["git", "-C", str(self.root), "rev-parse", "--absolute-git-dir"], text=True
        ).strip())
        read_root = self.root.parent / (self.root.name + "-read-only")
        read_root.mkdir()
        import shutil
        self.addCleanup(shutil.rmtree, read_root, True)
        effective = self.root / "effective.json"
        effective.write_bytes(b"immutable launch effective bytes\n")
        original = effective.read_bytes()
        capture = self.root / "permission-configs.jsonl"
        self.program("""
import json, os
with open(os.environ['PERMISSION_CONFIGS'], 'a', encoding='utf-8') as out:
 out.write(json.dumps({'config':json.loads(os.environ['OPENCODE_CONFIG_CONTENT'])}) + '\\n')
event('step_start', part={})
event('text', part={'text':'artifact: -\\nverdict: PASS\\nblocker: none'})
event('step_finish', part={'reason':'stop'})
""")
        context = {
            "adapter": "opencode", "attempt_id": self.args.parent_attempt_id,
            "worker_type": "owner", "dispatch_depth": 1, "capability": "autopilot-code",
            "worktree": str(self.root), "route_file": "", "route_id": "", "route_hash": "",
            "artifact_root": str(self.root / ".agent_reports"), "artifact_output_dir": "",
            "agent_home": str(ROOT), "opencode_config_base": json.dumps({"permission": {"bash": "allow"}}),
            "report_bundle_root": None, "execution_access_roots": [],
            "execution_access_read_roots": [str(read_root)], "contract_read_roots": [],
            "selected_agent": "build",
        }
        args = SimpleNamespace(
            runtime_harness="opencode", parent_attempt_id=self.args.parent_attempt_id,
            worktree=str(self.root), jobs=self.args.jobs, turn_timeout=2,
            opencode_command=shlex.join([sys.executable, str(self.script)]),
            model=None, variant=None, opencode_agent="build", owner_runtime_turn_ordinal=0,
        )
        for version in ("1.19.0", "2.0.32"):
            with self.subTest(version=version):
                capture.write_text("", encoding="utf-8")
                events = []
                args.owner_runtime_turn_ordinal = 0
                with unittest.mock.patch.dict(os.environ, {
                    "PERMISSION_CONFIGS": str(capture), "OPENCODE_RUNTIME_VERSION": version,
                }), unittest.mock.patch.object(
                    commit_policy, "read_runtime_permission_context", return_value=context), \
                     unittest.mock.patch.object(
                         commit_policy, "runtime_permission_context_is_current", return_value=True), \
                     unittest.mock.patch.object(fixture.supervisor, "emit", side_effect=events.append):
                    for ordinal, active in enumerate((False, True, False), 1):
                        marker = git_dir / "MERGE_HEAD"
                        if active:
                            marker.write_text("fixture\n", encoding="utf-8")
                        else:
                            marker.unlink(missing_ok=True)
                        args.owner_runtime_turn_ordinal = ordinal
                        fixture.supervisor.run_turn(args, "ignored-session", "continue", resume=ordinal > 1)

                configs = [json.loads(line)["config"] for line in capture.read_text().splitlines()]
                rows = [item for item in events
                        if item.get("type") == "dispatch.supervisor.permission-projection"]
                self.assertEqual(len(configs), 3)
                decoded = []
                for index, config in enumerate(configs):
                    if version.startswith("2."):
                        self.assertNotIn("permission", config)
                        self.assertNotIn("agent", config)
                        self.assertIn("permissions", config)
                        self.assertIn("agents", config)
                    legacy = (permission_runtime.opencode_v2_to_legacy_config(config)
                              if "permissions" in config else config)
                    recorded = dispatch_replacement._applied_permissions(
                        SimpleNamespace(opencode_config_content=json.dumps(config)))
                    permissions = recorded["opencode_permission"]
                    self.assertTrue(permission_runtime.opencode_permission_ordered_view(permissions))
                    self.assertEqual(permissions["external_directory"][str(read_root)], "allow")
                    edit = permissions["edit"]
                    if index == 1:
                        self.assertEqual(edit["*"], "deny")
                    else:
                        self.assertTrue(edit.get(str(read_root)) == "deny"
                                        or edit.get(str(read_root) + "/**") == "deny", edit)
                    decoded.append(permissions)
                self.assertEqual(decoded[0]["edit"]["*"], "allow")
                self.assertEqual(decoded[1]["edit"]["*"], "deny")
                self.assertEqual(decoded[2]["edit"]["*"], "allow")
                self.assertEqual([(item["attempt_id"], item["thread_id"], item["turn_id"]) for item in rows],
                                 [(self.args.parent_attempt_id, "ses_native", str(index)) for index in (1, 2, 3)])
                self.assertEqual([item["git_operation"] for item in rows], ["none", "merge", "none"])
                self.assertEqual([item["file_enforcement"] for item in rows], ["tool-permission"] * 3)
                self.assertEqual([item["network_enforcement"] for item in rows], ["none"] * 3)
        self.assertEqual(effective.read_bytes(), original)

    def test_exit_before_final_stop_is_not_success(self):
        self.program("event('text', part={'text':'artifact: -\\nverdict: PASS\\nblocker: none'})\n")
        with self.assertRaisesRegex(runtime.OpenCodeTransportError, 'final-stop-missing'):
            runtime.run_turn(self.args, 'start', emit=self.events.append)

    def test_record_accessor_preserves_string_absence_and_source_metadata(self):
        self.assertIsNone(runtime.read_binding_record(self.args))
        self.assertEqual(runtime.read_binding(self.args), "")
        runtime.bind_session(self.args, "ses_original")
        path = runtime.binding_path(self.args)
        path.chmod(0o664)
        record = runtime.read_binding_record(self.args)
        self.assertEqual(record["value"]["session_id"], runtime.read_binding(self.args))
        self.assertEqual(record["source"]["ctime"], path.stat().st_ctime)
        self.assertLessEqual(record["source"]["mtime"], time.time())

    def test_unsafe_source_types_and_generation_never_become_bindings(self):
        runtime.bind_session(self.args, "ses_original")
        path = runtime.binding_path(self.args)
        original = path.read_bytes()
        for raw in (b"false", b"null", b"[]", b"{}", b"{", b"\xff"):
            path.write_bytes(raw)
            with self.assertRaisesRegex(runtime.OpenCodeTransportError, "binding-invalid"):
                runtime.read_binding(self.args)
        path.write_bytes(b" " * 4097)
        with self.assertRaisesRegex(runtime.OpenCodeTransportError, "binding-unsafe"):
            runtime.read_binding(self.args)
        path.unlink()
        os.mkfifo(path)
        with self.assertRaisesRegex(runtime.OpenCodeTransportError, "binding-unsafe"):
            runtime.read_binding(self.args)
        path.unlink()
        path.write_bytes(original)
        uid = os.getuid()
        with unittest.mock.patch.object(runtime.os, "getuid", return_value=uid + 1):
            with self.assertRaisesRegex(runtime.OpenCodeTransportError, "binding-unsafe"):
                runtime.read_binding(self.args)
        with unittest.mock.patch.object(runtime.os, "open", side_effect=PermissionError()):
            with self.assertRaisesRegex(runtime.OpenCodeTransportError, "binding-invalid"):
                runtime.read_binding(self.args)
        real = os.fstat
        calls = 0
        def replace(fd):
            nonlocal calls
            calls += 1
            result = real(fd)
            if calls == 2:
                other = path.with_suffix(".new")
                other.write_bytes(original)
                os.replace(other, path)
            return result
        with unittest.mock.patch.object(runtime.os, "fstat", side_effect=replace):
            with self.assertRaisesRegex(runtime.OpenCodeTransportError, "binding-unsafe"):
                runtime.read_binding(self.args)

    def test_expected_worktree_is_independent_of_native_payload(self):
        runtime.bind_session(self.args, "ses_original")
        path = runtime.binding_path(self.args)
        row = json.loads(path.read_text())
        row["worktree"] = str(self.root / "namesake")
        path.write_text(json.dumps(row))
        with self.assertRaisesRegex(runtime.OpenCodeTransportError, "binding-invalid"):
            runtime.read_binding_record(self.args)

    def test_native_capacity_error_survives_process_exit(self):
        from dispatch_supervisor_terminal import classify_session_result
        self.program("event('error', error={'message':'rate limited', 'status':429})\nsys.exit(1)\n")
        result, code = runtime.run_turn(self.args, 'start', emit=self.events.append)
        terminal = classify_session_result(result, code, runtime='opencode')
        self.assertEqual(terminal.failure_class, 'capacity')
        self.assertEqual(terminal.terminal_event, 'opencode-result')

    def test_timeout_reaps_the_native_process(self):
        self.args.turn_timeout = .15
        self.program("from pathlib import Path\nimport os\nPath('pid').write_text(str(os.getpid()))\nevent('step_start', part={})\ntime.sleep(10)\n")
        with self.assertRaisesRegex(runtime.OpenCodeTransportError, 'turn-timeout'):
            runtime.run_turn(self.args, 'start', emit=self.events.append)
        pid = int((self.root/'pid').read_text())
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)


class ActiveTurnDeadlineTest(unittest.TestCase):
    """The native turn timeout is a no-progress window for a general owner
    turn (any event restarts it); silence, a real exit, a session mismatch and
    a sealed handoff keep their existing behaviour. These drive the real
    supervisor `run_turn`, which is what decides the policy."""

    WINDOW = 0.6
    READINESS_HANG_GUARD = 5
    CHATTY = """
from pathlib import Path
import os
Path('pid').write_text(str(os.getpid()))
ack = int(os.environ['HEARTING_ACK_FD'])
time.sleep(float(os.environ.get('HEARTING_START_DELAY', '0')))
ack_delay = float(os.environ.get('HEARTING_ACK_DELAY', '0'))
def paced(kind, **values):
    global ack_delay
    event(kind, **values)
    os.read(ack, 1)
    if ack_delay:
        time.sleep(ack_delay)
        ack_delay = 0
paced('step_start', part={})
for _ in range(6):
    paced('tool_use', part={'tool':'bash', 'callID':'c', 'state':{'status':'running'}})
paced('text', part={'text':'artifact: -\\nverdict: PASS\\nblocker: none'})
paced('step_finish', part={'reason':'stop'})
"""

    program = NativeSessionTest.program

    def setUp(self):
        NativeSessionTest.setUp(self)
        self.args.turn_timeout = self.WINDOW
        self.args.runtime_harness = "opencode"
        self.supervisor = fixture.supervisor
        patch = unittest.mock.patch.object(self.supervisor, "emit")
        patch.start()
        self.addCleanup(patch.stop)

    def turn(self, **kwargs):
        return self.supervisor.run_turn(self.args, "sess", "go", resume=False, **kwargs)

    def virtual_turn(self, *, startup_delay=0, ack_delay=0, **kwargs):
        clock = SimpleNamespace(now=0.0)
        original_selector = selectors.DefaultSelector
        readiness_hang_guard = self.READINESS_HANG_GUARD

        class Clock:
            @staticmethod
            def monotonic():
                return clock.now

        class ControlledSelector:
            def __init__(self):
                self.inner = original_selector()

            def __enter__(self):
                self.inner.__enter__()
                return self

            def __exit__(self, *args):
                return self.inner.__exit__(*args)

            def register(self, *args, **kw):
                return self.inner.register(*args, **kw)

            def select(self, timeout=None):
                # Real startup/ACK scheduling has its own hang guard. The
                # production timeout argument controls only the virtual clock.
                ready = self.inner.select(readiness_hang_guard)
                # A ready chunk advances virtual time by less than the idle window;
                # silence consumes the whole remaining deadline. Real readiness and
                # the real subprocess pipe remain in use.
                clock.now += min(0.3, timeout or 0.0) if ready else (timeout or 0.0)
                return ready

            def close(self):
                return self.inner.close()

        read_fd, write_fd = os.pipe()
        self.addCleanup(os.close, read_fd)
        self.addCleanup(os.close, write_fd)
        original_popen = runtime.subprocess.Popen

        def controlled_popen(*args, **kw):
            env = dict(os.environ)
            env['HEARTING_ACK_FD'] = str(read_fd)
            env['HEARTING_START_DELAY'] = str(startup_delay)
            env['HEARTING_ACK_DELAY'] = str(ack_delay)
            kw.update(pass_fds=(read_fd,), env=env)
            return original_popen(*args, **kw)

        real_read = os.read

        def acknowledged_read(fd, size):
            chunk = real_read(fd, size)
            if chunk and fd != read_fd:
                os.write(write_fd, b'!')
            return chunk

        with unittest.mock.patch.object(runtime, 'time', Clock()), \
             unittest.mock.patch.object(runtime.selectors, 'DefaultSelector', ControlledSelector), \
             unittest.mock.patch.object(runtime.subprocess, 'Popen', controlled_popen), \
             unittest.mock.patch.object(runtime.os, 'read', acknowledged_read):
            return self.turn(**kwargs), clock

    def test_native_live_turn_with_events_survives_past_the_window(self):
        self.program(self.CHATTY)
        (result, code), clock = self.virtual_turn()
        self.assertGreater(clock.now, self.WINDOW)
        self.assertEqual(code, 0)
        self.assertEqual(result["result"], "artifact: -\nverdict: PASS\nblocker: none")

    def test_real_startup_and_ack_delays_do_not_consume_virtual_deadline_under_load(self):
        # Delay both real readiness boundaries beyond the semantic window.
        # The load process stays owned by this fixture and is always reaped.
        load = subprocess.Popen([sys.executable, '-c', 'while True: pass'],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            self.program(self.CHATTY)
            (result, code), clock = self.virtual_turn(
                startup_delay=self.WINDOW * 1.5, ack_delay=self.WINDOW * 1.5)
            self.assertGreater(clock.now, self.WINDOW)
            self.assertEqual(code, 0)
            self.assertEqual(result['result'], 'artifact: -\nverdict: PASS\nblocker: none')
            with self.assertRaises(ProcessLookupError):
                os.kill(int((self.root / 'pid').read_text()), 0)
        finally:
            load.terminate()
            load.wait(timeout=5)

    def test_native_silence_for_the_whole_window_still_times_out_and_reaps(self):
        self.program("from pathlib import Path\nimport os\nPath('pid').write_text(str(os.getpid()))\n"
                     "event('step_start', part={})\ntime.sleep(10)\n")
        with self.assertRaisesRegex(self.supervisor.SupervisorError, "opencode-turn-timeout"):
            self.virtual_turn()
        with self.assertRaises(ProcessLookupError):
            os.kill(int((self.root / "pid").read_text()), 0)

    def test_native_post_eof_wait_stays_bounded(self):
        self.program("from pathlib import Path\nimport os\nPath('pid').write_text(str(os.getpid()))\n"
                     "event('step_start', part={})\nos.close(1)\ntime.sleep(10)\n")
        started = time.monotonic()
        with self.assertRaisesRegex(self.supervisor.SupervisorError, "opencode-turn-timeout"):
            self.turn()
        self.assertLess(time.monotonic() - started, 5)
        with self.assertRaises(ProcessLookupError):
            os.kill(int((self.root / "pid").read_text()), 0)

    def test_real_exit_before_final_stop_still_fails(self):
        self.program("event('text', part={'text':'x'})\n")
        with self.assertRaisesRegex(self.supervisor.SupervisorError, "final-stop-missing"):
            self.turn()

    def test_session_mismatch_still_fails(self):
        runtime.bind_session(self.args, "ses_original")
        self.program("event('step_start', part={})\n")
        with self.assertRaisesRegex(self.supervisor.SupervisorError, "identity-mismatch"):
            self.turn()

    def test_a_sealed_handoff_keeps_the_fixed_deadline_even_with_events(self):
        self.program(self.CHATTY)
        with self.assertRaisesRegex(self.supervisor.SupervisorError, "opencode-turn-timeout"):
            self.virtual_turn(handoff_intent={"intent_id": "handoff"})

    def test_silent_owner_turn_survives_when_exact_tool_activity_is_observed(self):
        from foreground_activity import ActivityObservation
        from session_supervisor_decisions import OwnerTurnActivity
        self.program(
            "time.sleep(0.9)\n"
            "event('text', part={'text':'done'})\n"
            "event('step_finish', part={'reason':'stop'})\n"
        )
        activity = OwnerTurnActivity(
            lambda: ActivityObservation(True, False, ("exact-tool-cpu",)),
            window_seconds=self.WINDOW, sample_seconds=0.05,
        )
        with mock.patch("foreground_activity.owner_turn_activity", return_value=activity):
            started = time.monotonic()
            result, code = self.turn(ordinary_owner_turn=True)
        self.assertGreater(time.monotonic() - started, self.WINDOW)
        self.assertEqual((result["result"], code), ("done", 0))

    def test_owner_transport_ignores_bookkeeping_output_and_expires_confirmed_idle(self):
        from foreground_activity import ActivityObservation
        from session_supervisor_decisions import OwnerTurnActivity
        self.program("print('supervisor bookkeeping', flush=True)\ntime.sleep(10)\n")
        activity = OwnerTurnActivity(
            lambda: ActivityObservation(False, False, ("quiet",)),
            window_seconds=self.WINDOW, sample_seconds=0.05,
        )
        with mock.patch("foreground_activity.owner_turn_activity", return_value=activity):
            with self.assertRaisesRegex(self.supervisor.SupervisorError, "opencode-turn-timeout"):
                self.turn(ordinary_owner_turn=True)

    def test_owner_activity_also_applies_after_eof_while_native_process_lives(self):
        from foreground_activity import ActivityObservation
        from session_supervisor_decisions import OwnerTurnActivity
        self.program(
            "import os\n"
            "event('step_start', part={})\n"
            "os.close(1)\n"
            "time.sleep(0.9)\n"
        )
        activity = OwnerTurnActivity(
            lambda: ActivityObservation(True, False, ("exact-tool-cpu",)),
            window_seconds=self.WINDOW, sample_seconds=0.05,
        )
        with mock.patch("foreground_activity.owner_turn_activity", return_value=activity):
            started = time.monotonic()
            with self.assertRaisesRegex(self.supervisor.SupervisorError, "opencode-final-stop-missing"):
                self.turn(ordinary_owner_turn=True)
        self.assertGreater(time.monotonic() - started, self.WINDOW)


class SharedControllerTest(unittest.TestCase):
    def setUp(self):
        self.f = fixture.ClaudeSessionSupervisorTest('test_resume_uses_same_session_once_after_join')
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)

    def test_actual_lease_is_required_for_every_harness(self):
        f = self.f
        request = SimpleNamespace(parent_attempt_id=fixture.PARENT, jobs=str(f.jobs))
        for harness, delivery in [('claude','session-resume-supervised'),
                                 ('codex','app-server-supervised'),
                                 ('opencode','session-resume-supervised')]:
            with self.subTest(harness=harness):
                f.jobs.write_text(fixture.owner_row(f.lease).replace('harness=claude', 'harness='+harness)
                                  .replace('completion_delivery=session-resume-supervised', 'completion_delivery='+delivery))
                self.assertFalse(parent.parent_supervisor_is_live(request))
                with hold_supervisor_lease(f.jobs, fixture.PARENT, f.lease):
                    self.assertTrue(parent.parent_supervisor_is_live(request))
                    other = SimpleNamespace(parent_attempt_id='att-other', jobs=str(f.jobs))
                    self.assertFalse(parent.parent_supervisor_is_live(other))
                self.assertFalse(parent.parent_supervisor_is_live(request))

    def test_common_join_resumes_the_same_native_session_and_commits_once(self):
        f = self.f
        native = f.base/'native.py'
        native.write_text("""
import json, os, sys
session = sys.argv[sys.argv.index('--session') + 1] if '--session' in sys.argv else 'ses_actual'
prompt = sys.stdin.read()
with open(sys.argv[sys.argv.index('--fixture-state') + 1]) as s:
 state = json.load(s)
 assert state['phase'] == 'running-turn', state
 if '--session' in sys.argv:
  assert state['outbox']['attempt_ids'] and not state['outbox'].get('consumed_attempt_ids'), state
with open(os.environ['FAKE_TRACE'], 'a') as f:
 f.write(json.dumps({'event':'native-turn', 'session':session, 'resume':'--session' in sys.argv, 'prompt':prompt,
                      'config':os.environ.get('OPENCODE_CONFIG_CONTENT')}) + '\\n')
text = 'artifact: -\\nverdict: PASS\\nblocker: none' if '--session' in sys.argv else 'artifact: -\\nverdict: BLOCKED\\nblocker: registered child running; report follows on wake'
for kind, part in [('step_start',{}), ('text',{'text':text}), ('step_finish',{'reason':'stop'})]:
 print(json.dumps({'type':kind,'sessionID':session,'part':part}))
""")
        f.jobs.write_text(fixture.owner_row(f.lease).replace('harness=claude', 'harness=opencode')
                          + fixture.child_row())
        command = f.command() + ['--runtime-harness','opencode', '--opencode-command',
                                  shlex.join([sys.executable, str(native), '--fixture-state', str(f.state)])]
        result = subprocess.run(command, input='initial', text=True, capture_output=True,
                                env=f.child_env(FAKE_TRACE=str(f.trace),
                                                OPENCODE_CONFIG_CONTENT='{"provider":{"opencode-go":{"models":{"glm-5.3-flash":{"limit":{"context":600000,"input":480000,"output":8192}}}}}',
                                                XDG_CONFIG_HOME=str(f.base/'masked-config')), timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
        trace = [json.loads(line) for line in f.trace.read_text().splitlines()]
        self.assertEqual([t['event'] for t in trace], ['native-turn','join-start','join-end','native-turn'])
        turns = [t for t in trace if t['event']=='native-turn']
        self.assertEqual([t['session'] for t in turns], ['ses_actual','ses_actual'])
        self.assertEqual([t['resume'] for t in turns], [False, True])
        self.assertEqual(turns[0]['config'], turns[1]['config'])
        self.assertIn('"context":600000', turns[0]['config'])
        events = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual(sum(e['type']=='result' for e in events), 1)
        self.assertEqual(events[-1]['runtime'], 'opencode')
        self.assertIn('note=completed-supervisor', f.jobs.read_text())
        log = f.base/'attempt.opencode.jsonl'
        log.write_text(result.stdout)
        checked = subprocess.run([sys.executable, str(ROOT/'utilities/codex_dispatch_terminal.py'),
                                  '--worktree',str(f.base),'--artifact-root-metadata',str(f.artifact_root),str(log)],
                                 capture_output=True,text=True,env=f.child_env())
        self.assertEqual(checked.returncode, 0, checked.stdout+checked.stderr)
        self.assertIn('\tvalid\texact-opencode-result\tPASS', checked.stdout)

    def test_real_adapter_selects_controller_only_for_bound_owner(self):
        spec = importlib.util.spec_from_file_location('oc_adapter', ROOT/'adapters/opencode/bin/dispatch-headless.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        args = SimpleNamespace(owner_route_binding=SimpleNamespace(route_file='/route.json',route_id='rt-exact',route_hash='sha256:exact'),
            jobs_path=self.f.jobs, attempt_id=fixture.PARENT, worktree=str(self.f.base),agent='build',
            resolved_model_settings={'source':'profile','model':'opencode-go/glm-5.3-flash','variant':'runtime-default'})
        command = shlex.split(module.shell_command(args, self.f.base/'prompt', self.f.base/'log'))
        self.assertIn('--runtime-harness', command)
        self.assertEqual(command[command.index('--parent-attempt-id')+1], fixture.PARENT)
        self.assertEqual(command[command.index('--lease-file')+1], str(self.f.lease))
        args.attempt_id = None  # Public dry-run allocates neither row nor identity.
        preview = module.shell_command(args, self.f.base/'prompt', self.f.base/'log')
        self.assertIn('--parent-attempt-id unassigned', preview)
        self.assertIn('preview-only.lease', preview)
        args.owner_route_binding = None
        with unittest.mock.patch.dict(os.environ, {"OPENCODE_RUNTIME_VERSION": "1.19.0"}):
            self.assertTrue(module.shell_command(
                args, self.f.base/'prompt', self.f.base/'log').startswith('opencode run '))


    def test_input_queued_before_the_first_turn_binds_the_native_session(self):
        import dispatch_owner_input as owner_input
        f = self.f
        native = f.base/'native.py'
        native.write_text("""
import json, os, sys
prompt = sys.stdin.read()
with open(os.environ['FAKE_TRACE'], 'a') as f:
 f.write(json.dumps({'args':sys.argv[1:], 'prompt':prompt}) + '\\n')
text = 'artifact: -\\nverdict: PASS\\nblocker: none'
for kind, part in [('step_start',{}), ('text',{'text':text}), ('step_finish',{'reason':'stop'})]:
 print(json.dumps({'type':kind,'sessionID':'ses_actual','part':part}))
""")
        f.jobs.write_text(fixture.owner_row(f.lease).replace('harness=claude', 'harness=opencode'))
        owner_input.initialize_owner_input(f.jobs, fixture.PARENT, 'opencode-next-turn')
        owner_input.submit(f.jobs, fixture.PARENT, 'early word', 'early')
        command = f.command() + ['--runtime-harness', 'opencode', '--opencode-command', shlex.join([sys.executable, str(native)])]
        result = subprocess.run(command, input='initial', text=True, capture_output=True,
                                env=f.child_env(FAKE_TRACE=str(f.trace)), timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
        turns = [json.loads(line) for line in f.trace.read_text().splitlines()]
        self.assertEqual(len(turns), 1)
        self.assertIn('early word', turns[0]['prompt'])
        self.assertNotIn('--session', turns[0]['args'])
        receipt = owner_input.inspect(f.jobs, fixture.PARENT)
        self.assertEqual(receipt['requests'][0]['state'], 'turn-completed')
        self.assertEqual(receipt['thread_id'], 'ses_actual')

    def test_first_live_turn_can_bind_its_registered_resource_owner(self):
        f = self.f
        f.state = f.jobs.parent / 'supervisor-state' / (fixture.PARENT + '.json')
        f.jobs.write_text(fixture.owner_row(f.lease).replace('harness=claude', 'harness=opencode'))
        native = f.base / 'native-resource.py'
        native.write_text("""
import json, os, sys, time
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
sys.path.insert(0, os.environ['FAKE_UTILITIES'])
import dispatch_owner_input as INPUT
import dispatch_resource_wait as RESOURCE
import dispatch_contract as CONTRACT
import owner_route_binding as OWNER
sys.stdin.read()
session = 'ses_ee3219f52ffe32c72Oe1nJNaxc'
def event(kind, **values):
 print(json.dumps(dict(type=kind, sessionID=session, **values)), flush=True)
event('step_start', part={})
jobs = Path(os.environ['AGENT_DISPATCH_JOBS'])
attempt = 'att-parent'
deadline = time.monotonic() + 3
while True:
 current = INPUT.inspect(jobs, attempt)
 if current['thread_id'] == session:
  break
 if time.monotonic() >= deadline:
  raise AssertionError(('first live turn still has placeholder session', current))
 time.sleep(.01)
assert current['supervisor_live'], current
route_file = Path.cwd() / 'resource-route.json'
route = dict(route_id='rt-native-resource', route_hash='sha256:native-resource')
binding = SimpleNamespace(route_file=str(route_file), **route)
args = SimpleNamespace(jobs=str(jobs), parent_attempt_id=attempt)
completion_state = os.environ.get('AGENT_DISPATCH_COMPLETION_STATE_FILE', '')
env = {key:value for key,value in os.environ.items()
       if not key.startswith('AGENT_DISPATCH_')}
env.update(AGENT_DISPATCH_ATTEMPT_ID=attempt,
           AGENT_OWNER_ROUTE_FILE=str(route_file),
           AGENT_DISPATCH_COMPLETION_MODE='supervised',
           AGENT_DISPATCH_COMPLETION_STATE_FILE=completion_state)
with mock.patch.object(OWNER, 'resolve_owner_route_lifecycle', return_value=(binding, 'bound')), \\
     mock.patch.object(CONTRACT, 'resolve_live_parent_attempt',
                       return_value=SimpleNamespace(pid=os.getppid(), pid_start='200')):
 owner = RESOURCE.start_binding(route, route_file, args, env)
assert owner['session_id'] == session, owner
Path('resource-owner.json').write_text(json.dumps(owner))
event('text', part={'text':'artifact: -\\nverdict: PASS\\nblocker: none'})
event('step_finish', part={'reason':'stop'})
""")
        command = f.command() + ['--runtime-harness', 'opencode', '--opencode-command',
                                  shlex.join([sys.executable, str(native)])]
        result = subprocess.run(command, input='register full-run in this first turn',
                                text=True, capture_output=True, timeout=15,
                                env=f.child_env(FAKE_TRACE=str(f.trace),
                                                AGENT_DISPATCH_JOBS=str(f.jobs),
                                                AGENT_DISPATCH_COMPLETION_STATE_FILE=str(f.state),
                                                OPENCODE_RUNTIME_VERSION='2.0.32',
                                                FAKE_UTILITIES=str(ROOT / 'utilities')))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        owner = json.loads((f.base / 'resource-owner.json').read_text())
        self.assertEqual(owner['session_id'], 'ses_ee3219f52ffe32c72Oe1nJNaxc')
        self.assertEqual(owner['parent_attempt_id'], fixture.PARENT)


if __name__ == '__main__':
    unittest.main()
