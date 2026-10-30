#!/usr/bin/env python3
"""Read-only OpenCode runtime boundary fixtures; no native process or model calls."""
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "utilities"))
import opencode_runtime as runtime


def write_detached_version_cli(script: Path, pid_file: Path, *, hang: bool = False) -> None:
    detached = (
        "import os,pathlib,time\n"
        "if os.fork(): os._exit(0)\n"
        "os.setsid()\n"
        "if os.fork(): os._exit(0)\n"
        f"pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid()))\n"
        "time.sleep(30)\n"
    )
    source = (
        "#!/usr/bin/env python3\nimport pathlib,subprocess,sys,time\n"
        f"subprocess.Popen([sys.executable,'-c',{detached!r}],env={{}})\n"
        f"pid_file=pathlib.Path({str(pid_file)!r})\n"
        "deadline=time.monotonic()+2\n"
        "while not pid_file.exists() and time.monotonic()<deadline: time.sleep(.001)\n"
        "print('opencode 2.0.32',flush=True)\n"
        + ("time.sleep(30)\n" if hang else "")
    )
    script.write_text(source, encoding="utf-8")
    script.chmod(0o700)


class OpenCodeRunCodecTest(unittest.TestCase):
    def test_launch_version_probe_uses_private_store_and_rejects_bad_versions(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "dispatch"
            root.mkdir()
            script = Path(temp) / "opencode"
            script.write_text(
                "#!/usr/bin/env python3\n"
                "import os, pathlib, sys\n"
                f"store = pathlib.Path({str(root)!r})\n"
                "assert pathlib.Path(os.environ['HOME']).is_relative_to(store)\n"
                "assert pathlib.Path(os.environ['TMPDIR']).is_relative_to(store)\n"
                "assert os.environ['XDG_DATA_HOME'] != '/user/data'\n"
                "assert 'secret' not in os.environ\n"
                "print('opencode 2.0.32')\n",
                encoding="utf-8",
            )
            script.chmod(0o700)
            version = runtime.probe_launch_version(
                binary=str(script), cwd=temp, state_root=root,
                environ={"PATH": os.environ.get("PATH", ""), "HOME": "/user",
                         "XDG_DATA_HOME": "/user/data", "OPENAI_API_KEY": "secret"},
            )
            self.assertEqual(version, (2, 0, 32))
            self.assertEqual(list(root.iterdir()), [])

    def test_launch_version_probe_fails_closed_for_nonzero_or_malformed_output(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "dispatch"
            root.mkdir()
            script = Path(temp) / "opencode"
            for body in ("print('not a version')\n", "print('opencode 2.0.32')\nsys.exit(1)\n"):
                script.write_text("#!/usr/bin/env python3\nimport sys\n" + body, encoding="utf-8")
                script.chmod(0o700)
                with self.assertRaisesRegex(runtime.OpenCodeRuntimeError,
                                            "opencode-runtime-version-unavailable"):
                    runtime.probe_launch_version(binary=str(script), cwd=temp, state_root=root)

    def test_launch_version_probe_rejects_truncation_and_caps_capture_file(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "dispatch"
            root.mkdir()
            script = Path(temp) / "opencode"
            script.write_text(
                "#!/usr/bin/env python3\nimport os, pathlib, sys, time\n"
                "target = pathlib.Path(os.environ['HOME']).parent / 'version.out'\n"
                "sys.stdout.write('opencode 2.0.32\\n'); sys.stdout.flush()\n"
                "deadline = time.monotonic() + 1\n"
                "while target.stat().st_size < 16 and time.monotonic() < deadline: time.sleep(.001)\n"
                "sys.stdout.write('x' * (512 * 1024)); sys.stdout.flush()\n"
                "deadline = time.monotonic() + 1\n"
                "while target.stat().st_size < 32 and time.monotonic() < deadline: time.sleep(.001)\n"
                "time.sleep(.02)\n"
                "pathlib.Path.cwd().joinpath('capture-size.txt').write_text(str(target.stat().st_size))\n",
                encoding="utf-8",
            )
            script.chmod(0o700)
            with unittest.mock.patch.object(runtime, "VERSION_PROBE_OUTPUT_LIMIT", 32), \
                    self.assertRaisesRegex(runtime.OpenCodeRuntimeError,
                                           "opencode-runtime-version-unavailable"):
                runtime.probe_launch_version(binary=str(script), cwd=temp, state_root=root)
            self.assertEqual(int((Path(temp) / "capture-size.txt").read_text()), 32)
            script.write_text(
                "#!/usr/bin/env python3\nimport sys\n"
                "sys.stdout.write('x' * 64 + '\\nopencode 2.0.32\\n')\n",
                encoding="utf-8",
            )
            with unittest.mock.patch.object(runtime, "VERSION_PROBE_OUTPUT_LIMIT", 32), \
                    self.assertRaisesRegex(runtime.OpenCodeRuntimeError,
                                           "opencode-runtime-version-unavailable"):
                runtime.probe_launch_version(binary=str(script), cwd=temp, state_root=root)
            self.assertEqual(list(root.iterdir()), [])

    def test_launch_version_probe_reaps_double_fork_setsid_descendant_after_normal_exit(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "dispatch"
            root.mkdir()
            pid_file = Path(temp) / "detached-pid"
            script = Path(temp) / "opencode"
            write_detached_version_cli(script, pid_file)
            self.assertEqual(runtime.probe_launch_version(
                binary=str(script), cwd=temp, state_root=root), (2, 0, 32))
            pid = int(pid_file.read_text())
            self.assertFalse(Path(f"/proc/{pid}").exists())
            self.assertFalse(any(thread.name == "opencode-version-probe-output" and thread.is_alive()
                                 for thread in threading.enumerate()))
            self.assertEqual(list(root.iterdir()), [])

    def test_launch_version_probe_timeout_reaps_double_fork_setsid_descendant(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "dispatch"
            root.mkdir()
            pid_file = Path(temp) / "timeout-detached-pid"
            script = Path(temp) / "opencode"
            write_detached_version_cli(script, pid_file, hang=True)
            with self.assertRaisesRegex(runtime.OpenCodeRuntimeError,
                                        "opencode-runtime-version-unavailable"):
                runtime.probe_launch_version(binary=str(script), cwd=temp,
                                             state_root=root, timeout=0.1)
            pid = int(pid_file.read_text())
            self.assertFalse(Path(f"/proc/{pid}").exists())
            self.assertFalse(any(thread.name == "opencode-version-probe-output" and thread.is_alive()
                                 for thread in threading.enumerate()))
            self.assertEqual(list(root.iterdir()), [])

    def test_launch_version_probe_rejects_valid_output_from_a_stopped_lease(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "dispatch"
            root.mkdir()
            script = Path(temp) / "opencode"
            script.write_text(
                "#!/usr/bin/env python3\nimport os,signal,time\n"
                "print('opencode 2.0.32',flush=True)\n"
                "os.kill(os.getppid(),signal.SIGTERM)\n"
                "time.sleep(30)\n")
            script.chmod(0o700)
            with self.assertRaisesRegex(runtime.OpenCodeRuntimeError,
                                        "opencode-runtime-version-unavailable"):
                runtime.probe_launch_version(binary=str(script), cwd=temp, state_root=root)
            self.assertEqual(list(root.iterdir()), [])
            self.assertFalse(any(thread.name == "opencode-version-probe-output" and thread.is_alive()
                                 for thread in threading.enumerate()))

    def test_v1_flags_and_v2_provider_variant_are_separate(self):
        v1 = runtime.run_argv(binary="opencode", cwd="/work", agent="build",
                              model="opencode-go/model", variant="high", pure=True,
                              version=(1, 19, 0))
        self.assertIn("--pure", v1)
        self.assertIn("--dir", v1)
        self.assertIn("--variant", v1)
        v2 = runtime.run_argv(binary="opencode", cwd="/work", agent="build",
                              model="opencode-go/model", variant="high", session="ses_exact",
                              title="hearting-worker-att-x", pure=True, version=(2, 0, 32))
        self.assertNotIn("--pure", v2)
        self.assertNotIn("--standalone", v2)
        self.assertNotIn("--dir", v2)
        self.assertNotIn("--variant", v2)
        self.assertEqual(v2[v2.index("--model") + 1], "opencode-go/model#high")
        self.assertEqual(v2[v2.index("--session") + 1], "ses_exact")
        self.assertEqual(v2[v2.index("--title") + 1], "hearting-worker-att-x")
        private_v2 = runtime.run_argv(binary="opencode", model="fixture/model",
                                      standalone=True, version=(2, 0, 32))
        self.assertIn("--standalone", private_v2)
        self.assertEqual(private_v2[private_v2.index("--model") + 1], "fixture/model")
        self.assertNotIn("--standalone", runtime.run_argv(
            binary="opencode", model="fixture/model", standalone=True, version=(1, 19, 0)))

    def test_database_generation_never_selects_cli_generation(self):
        with unittest.mock.patch.dict(os.environ, {"OPENCODE_RUNTIME_VERSION": "1.19.0"}), \
             unittest.mock.patch.object(runtime, "credential_store_present", return_value=True):
            self.assertFalse(runtime.is_v2())
        with unittest.mock.patch.dict(os.environ, {}, clear=True), \
             unittest.mock.patch.object(runtime, "runtime_version", return_value=None), \
             unittest.mock.patch.object(runtime, "credential_store_present", return_value=True):
            self.assertFalse(runtime.is_v2())
        with unittest.mock.patch.dict(os.environ, {"OPENCODE_RUNTIME_VERSION": "2.0.32"}):
            self.assertTrue(runtime.is_v2())

    def test_v2_default_variant_has_no_suffix(self):
        self.assertEqual(runtime.model_reference("openai/gpt-6.1-sol", "runtime-default"),
                         "openai/gpt-6.1-sol")
        self.assertEqual(runtime.model_reference("opencode/model#old", "high"),
                         "opencode/model#high")


    @staticmethod
    def _cli(path, output="2.0.32", executable=True):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"#!/bin/sh\nprintf '{output}\\n'\n")
        path.chmod(0o755 if executable else 0o644)
        return path

    def test_v2_cli_resolves_from_fixture_home_when_missing_from_path(self):
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as empty_path:
            binary = Path(home) / ".opencode" / "bin" / "opencode"
            binary.parent.mkdir(parents=True)
            binary.write_text("#!/bin/sh\nprintf '2.0.32\\n'\n")
            binary.chmod(0o755)
            env = {"HOME": home, "PATH": empty_path}
            with unittest.mock.patch.dict(os.environ, {}, clear=True):
                self.assertTrue(runtime.is_v2(environ=env))
                self.assertEqual(runtime.resolve_binary(environ=env), str(binary))
                self.assertFalse(runtime.is_v2(binary="injected-opencode", environ=env))
                self.assertFalse(runtime.is_v2(environ=env, version=(1, 19, 0)))

    def test_ambient_public_version_api_resolves_fixture_home_without_path_binary(self):
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as empty_path:
            binary = Path(home) / ".opencode" / "bin" / "opencode"
            binary.parent.mkdir(parents=True)
            binary.write_text("#!/bin/sh\nprintf '2.0.32\\n'\n")
            binary.chmod(0o755)
            runtime.runtime_version.cache_clear()
            with unittest.mock.patch.dict(os.environ, {"HOME": home, "PATH": empty_path}, clear=True):
                self.assertEqual(runtime.runtime_version(), (2, 0, 32))
                runtime.runtime_version.cache_clear()
                self.assertTrue(runtime.is_v2())
            runtime.runtime_version.cache_clear()

    def test_explicit_argument_wins_over_opencode_bin(self):
        with tempfile.TemporaryDirectory() as temp:
            chosen = self._cli(Path(temp) / "chosen" / "opencode", "1.19.0")
            override = self._cli(Path(temp) / "override" / "opencode", "2.0.32")
            env = {"HOME": temp, "PATH": "", "OPENCODE_BIN": str(override)}
            self.assertEqual(runtime.resolve_binary(str(chosen), environ=env), str(chosen))

    def test_opencode_bin_wins_over_path_and_home_and_is_probed(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp) / "home"
            path_dir = Path(temp) / "path"
            local = self._cli(home / ".opencode/bin/opencode", "1.0.0")
            on_path = self._cli(path_dir / "opencode", "1.0.0")
            selected = self._cli(Path(temp) / "selected/opencode", "2.1.0")
            env = {"HOME": str(home), "PATH": str(path_dir), "OPENCODE_BIN": str(selected)}
            with unittest.mock.patch.dict(os.environ, {}, clear=True):
                self.assertEqual(runtime.resolve_binary(environ=env), str(selected))
                self.assertTrue(runtime.is_v2(environ=env))
            self.assertTrue(local.is_file() and on_path.is_file())

    def test_invalid_opencode_bin_does_not_fall_back(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp) / "home"
            path_dir = Path(temp) / "path"
            self._cli(home / ".opencode/bin/opencode")
            self._cli(path_dir / "opencode")
            missing = Path(temp) / "missing/opencode"
            env = {"HOME": str(home), "PATH": str(path_dir), "OPENCODE_BIN": str(missing)}
            with unittest.mock.patch.dict(os.environ, {}, clear=True):
                self.assertEqual(runtime.resolve_binary(environ=env), str(missing))
                self.assertFalse(runtime.is_v2(environ=env))

    def test_path_precedes_home_and_home_requires_executable_regular_file(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp) / "home"
            path_dir = Path(temp) / "path"
            self._cli(home / ".opencode/bin/opencode", "1.0.0")
            path_cli = self._cli(path_dir / "opencode", "2.0.0")
            env = {"HOME": str(home), "PATH": str(path_dir)}
            self.assertEqual(runtime.resolve_binary(environ=env), str(path_cli))
            missing_env = {"HOME": str(Path(temp) / "missing-home"), "PATH": ""}
            self.assertEqual(runtime.resolve_binary(environ=missing_env), "opencode")
            self._cli(Path(missing_env["HOME"]) / ".opencode/bin/opencode", executable=False)
            self.assertEqual(runtime.resolve_binary(environ=missing_env), "opencode")

    def test_unknown_version_and_timeout_are_unknown_with_bounded_probe(self):
        env = {"PATH": "", "HOME": ""}
        with unittest.mock.patch.dict(os.environ, {}, clear=True), \
             unittest.mock.patch.object(runtime.subprocess, "run", return_value=
                                        unittest.mock.Mock(stdout="not a version", stderr="")) as run:
            self.assertIsNone(runtime._runtime_version_for("opencode", env))
            run.assert_called_once()
            self.assertEqual(run.call_args.kwargs["timeout"], 3)
            runtime.runtime_version.cache_clear()
            run.side_effect = subprocess.TimeoutExpired(["opencode", "--version"], 3)
            self.assertIsNone(runtime._runtime_version_for("opencode", env))

    def test_custom_opencode_bin_name_is_never_probed(self):
        with tempfile.TemporaryDirectory() as temp:
            marker = Path(temp) / "executed"
            wrapper = Path(temp) / "wrapper"
            wrapper.write_text(f"#!/bin/sh\ntouch '{marker}'\nprintf '2.0.0\\n'\n")
            wrapper.chmod(0o755)
            # A basename other than opencode is not CLI evidence, even when
            # selected through OPENCODE_BIN from the default is_v2() call.
            env = {"PATH": "", "HOME": temp, "OPENCODE_BIN": str(wrapper)}
            with unittest.mock.patch.dict(os.environ, {}, clear=True):
                self.assertFalse(runtime.is_v2(environ=env))
            self.assertFalse(marker.exists())

    def test_v1_flags_and_v2_provider_variant_are_separate(self):
        v1 = runtime.run_argv(binary="opencode", cwd="/work", agent="build",
                              model="opencode-go/model", variant="high", pure=True,
                              version=(1, 19, 0))
        self.assertIn("--pure", v1)
        self.assertIn("--dir", v1)
        self.assertIn("--variant", v1)
        v2 = runtime.run_argv(binary="opencode", cwd="/work", agent="build",
                              model="opencode-go/model", variant="high", session="ses_exact",
                              title="hearting-worker-att-x", pure=True, version=(2, 0, 32))
        self.assertNotIn("--pure", v2)
        self.assertNotIn("--standalone", v2)
        self.assertNotIn("--dir", v2)
        self.assertNotIn("--variant", v2)
        self.assertEqual(v2[v2.index("--model") + 1], "opencode-go/model#high")
        self.assertEqual(v2[v2.index("--session") + 1], "ses_exact")
        self.assertEqual(v2[v2.index("--title") + 1], "hearting-worker-att-x")
        private_v2 = runtime.run_argv(binary="opencode", model="fixture/model",
                                      standalone=True, version=(2, 0, 32))
        self.assertIn("--standalone", private_v2)
        self.assertEqual(private_v2[private_v2.index("--model") + 1], "fixture/model")
        self.assertNotIn("--standalone", runtime.run_argv(
            binary="opencode", model="fixture/model", standalone=True, version=(1, 19, 0)))

    def test_database_generation_never_selects_cli_generation(self):
        with unittest.mock.patch.dict(os.environ, {"OPENCODE_RUNTIME_VERSION": "1.19.0"}), \
             unittest.mock.patch.object(runtime, "credential_store_present", return_value=True):
            self.assertFalse(runtime.is_v2())
        with unittest.mock.patch.dict(os.environ, {}, clear=True), \
             unittest.mock.patch.object(runtime, "runtime_version", return_value=None), \
             unittest.mock.patch.object(runtime, "credential_store_present", return_value=True):
            self.assertFalse(runtime.is_v2())
        with unittest.mock.patch.dict(os.environ, {"OPENCODE_RUNTIME_VERSION": "2.0.32"}):
            self.assertTrue(runtime.is_v2())



class OpenCodeFullPartsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / "opencode.db"

    def test_v1_rowid_cursor_and_mutable_refresh(self):
        sid = "ses_fullv1"
        with sqlite3.connect(self.db) as con:
            con.execute("CREATE TABLE message (id TEXT, session_id TEXT, role TEXT)")
            con.execute("CREATE TABLE part (session_id TEXT, message_id TEXT, data TEXT)")
            con.execute("INSERT INTO message VALUES ('m1', ?, 'assistant')", (sid,))
            con.execute("INSERT INTO part VALUES (?, 'm1', ?)", (sid, json.dumps({"type":"text","text":"x"*3000})))
        row = runtime.read_session_parts(sid, db_path=self.db)
        self.assertEqual(row["generation"], "v1")
        self.assertEqual(len(row["parts"][0]["data"]["text"]), 3000)
        with sqlite3.connect(self.db) as con:
            con.execute("UPDATE part SET data=? WHERE rowid=1", (json.dumps({"type":"text","text":"complete"}),))
        updated = runtime.read_session_parts(sid, db_path=self.db, after_cursor=row["cursor"],
                                             refresh_cursors=[row["cursor"]])
        self.assertEqual(updated["parts"][0]["data"]["text"], "complete")

    def test_v2_seq_cursor_and_bad_json_are_preserved(self):
        sid = "ses_fullv2"
        with sqlite3.connect(self.db) as con:
            con.execute("CREATE TABLE session_message (session_id TEXT, type TEXT, seq INTEGER, data TEXT)")
            con.execute("INSERT INTO session_message VALUES (?, 'assistant', 1, ?)",
                        (sid, json.dumps({"content":[{"type":"text","text":"y"*3000}]})))
            con.execute("INSERT INTO session_message VALUES (?, 'assistant', 2, 'bad-json')", (sid,))
        row = runtime.read_session_parts(sid, db_path=self.db)
        self.assertEqual(row["generation"], "v2")
        self.assertEqual(len(row["parts"][0]["data"]["content"][0]["text"]), 3000)
        self.assertEqual(row["parts"][1]["raw"], "bad-json")

    def test_budget_arguments_bound_one_read_and_leave_the_default_unbounded(self):
        sid = "ses_budget"
        with sqlite3.connect(self.db) as con:
            con.execute("CREATE TABLE message (id TEXT, session_id TEXT, role TEXT)")
            con.execute("CREATE TABLE part (session_id TEXT, message_id TEXT, data TEXT)")
            con.execute("INSERT INTO message VALUES ('m1', ?, 'assistant')", (sid,))
            for index in range(1, 11):
                con.execute("INSERT INTO part VALUES (?, 'm1', ?)", (sid, json.dumps({"type": "text", "text": f"t{index}"})))
        everything = runtime.read_session_parts(sid, db_path=self.db)
        self.assertEqual([part["cursor"] for part in everything["parts"]], list(range(1, 11)))
        self.assertNotIn("first_cursor", everything)
        newest = runtime.read_session_parts(sid, db_path=self.db, limit=3, newest=True)
        self.assertEqual([part["cursor"] for part in newest["parts"]], [8, 9, 10])
        self.assertEqual((newest["cursor"], newest["first_cursor"]), (10, 8))
        forward = runtime.read_session_parts(sid, db_path=self.db, after_cursor=2, limit=3)
        self.assertEqual([part["cursor"] for part in forward["parts"]], [3, 4, 5])
        before = runtime.read_session_parts(sid, db_path=self.db, before_cursor=8, limit=3)
        self.assertEqual([part["cursor"] for part in before["parts"]], [5, 6, 7])
        self.assertEqual(before["first_cursor"], 5)
        refreshed = runtime.read_session_parts(sid, db_path=self.db, after_cursor=8, limit=1, refresh_cursors=[2])
        self.assertEqual([part["cursor"] for part in refreshed["parts"]], [2, 9])

    def test_budget_arguments_work_on_the_v2_generation_too(self):
        sid = "ses_budgetv2"
        with sqlite3.connect(self.db) as con:
            con.execute("CREATE TABLE session_message (session_id TEXT, type TEXT, seq INTEGER, data TEXT)")
            for seq in range(1, 6):
                con.execute("INSERT INTO session_message VALUES (?, 'user', ?, '{}')", (sid, seq))
        self.assertEqual([p["cursor"] for p in runtime.read_session_parts(sid, db_path=self.db, limit=2, newest=True)["parts"]], [4, 5])
        self.assertEqual([p["cursor"] for p in runtime.read_session_parts(sid, db_path=self.db, before_cursor=4, limit=2)["parts"]], [2, 3])

    def test_generation_ambiguity_and_missing_database_are_not_guessed(self):
        sid = "ses_ambiguous"
        with sqlite3.connect(self.db) as con:
            con.execute("CREATE TABLE message (id TEXT, session_id TEXT, role TEXT)")
            con.execute("CREATE TABLE part (session_id TEXT, message_id TEXT, data TEXT)")
            con.execute("CREATE TABLE session_message (session_id TEXT, type TEXT, seq INTEGER, data TEXT)")
            con.execute("INSERT INTO part VALUES (?, 'm', '{}')", (sid,))
            con.execute("INSERT INTO session_message VALUES (?, 'user', 1, '{}')", (sid,))
        self.assertEqual(runtime.read_session_parts(sid, db_path=self.db)["reason"],
                         "session-generation-ambiguous")
        self.assertEqual(runtime.read_session_parts("ses_missing", db_path=self.db.with_name("missing.db"))["state"],
                         "missing")


class OpenCodePermissionCodecTest(unittest.TestCase):
    @staticmethod
    def reference_effect(rows, action, resource):
        """Independent V2 last-match oracle for the simple documented wildcard grammar."""
        import fnmatch
        effect = None
        for row in rows:
            if fnmatch.fnmatchcase(action, row["action"]) and fnmatch.fnmatchcase(
                    resource, row["resource"]):
                effect = row["effect"]
        return effect

    def check_projection(self, rows, actions, resources):
        config = {"permissions": rows}
        projected = runtime.opencode_v2_to_legacy_config(config)
        for action in actions:
            for resource in resources:
                expected = self.reference_effect(rows, action, resource)
                actual = None
                for pattern, effect in runtime.opencode_permission_rules_for_action(
                        projected["permission"], action) or ():
                    import fnmatch
                    if fnmatch.fnmatchcase(resource, pattern):
                        actual = effect
                self.assertEqual(actual, expected, (action, resource, rows, projected))

    def test_scalar_object_and_action_aliases_share_the_legacy_comparison_view(self):
        self.assertEqual(runtime.opencode_permission_ordered_view("allow"),
                         runtime.opencode_permission_ordered_view({"*": "allow"}))
        self.assertEqual(runtime.opencode_permission_ordered_view({"bash": "allow"}),
                         runtime.opencode_permission_ordered_view(
                             {"bash": {"*": "allow"}}))
        rows = runtime.opencode_legacy_to_v2_config({"permission": {
            "write": {"/approved/**": "allow"},
            "patch": {"/approved/**": "deny"},
            "edit": {"*": "deny"},
            "bash": "allow", "task": {"agent/*": "deny"},
        }})["permissions"]
        self.assertEqual([row["action"] for row in rows],
                         ["edit", "edit", "edit", "shell", "subagent"])
        self.assertEqual(self.reference_effect(rows, "edit", "/approved/file"), "deny")
        self.assertEqual(self.reference_effect(rows, "shell", "git status"), "allow")
        self.assertEqual(self.reference_effect(rows, "subagent", "agent/build"), "deny")

    def test_legacy_bash_command_shorthand_overlap_stays_order_sensitive(self):
        first = runtime.opencode_permission_ordered_view({
            "bash *": {"*": "deny"}, "bash": {"*": "allow"},
        })
        second = runtime.opencode_permission_ordered_view({
            "bash": {"*": "allow"}, "bash *": {"*": "deny"},
        })
        self.assertNotEqual(first, second)

    def test_redundant_equal_effect_and_shadowed_rules_compare_equal(self):
        deny = runtime.opencode_permission_ordered_view({"edit": "deny"})
        for rules in (
                {"*": "deny", "/root/**": "deny"},
                {"/root/**": "deny", "*": "deny"},
                {"/root/**": "allow", "*": "deny"}):
            with self.subTest(rules=rules):
                self.assertEqual(runtime.opencode_permission_ordered_view({"edit": rules}),
                                 deny)
        self.assertEqual(runtime.opencode_permission_ordered_view({
            "*": "deny", "edit": {"/root/**": "deny"},
        }), runtime.opencode_permission_ordered_view({"*": "deny"}))
        self.assertEqual(runtime.opencode_permission_ordered_view({
            "edit": {"/root/**": "allow"}, "*": "deny",
        }), runtime.opencode_permission_ordered_view({"*": "deny"}))
        overlapping = {"/root/*": "deny", "*/file": "deny"}
        self.assertEqual(runtime.opencode_permission_ordered_view({"edit": overlapping}),
                         runtime.opencode_permission_ordered_view({
                             "edit": dict(reversed(list(overlapping.items())))}))

    def test_conflicting_overlaps_without_containment_stay_order_sensitive(self):
        rules = {"/root/*": "deny", "*/file": "allow"}
        first, second = {"edit": rules}, {"edit": dict(reversed(list(rules.items())))}
        self.assertNotEqual(runtime.opencode_permission_ordered_view(first),
                            runtime.opencode_permission_ordered_view(second))
        self.assertEqual(runtime.opencode_permission_effect(first, "edit", "/root/file"), "allow")
        self.assertEqual(runtime.opencode_permission_effect(second, "edit", "/root/file"), "deny")

    def test_pruning_keeps_a_deny_after_an_intervening_allow(self):
        restricted = {"edit": {"*": "deny", "/root/**": "allow",
                               "/root/private/**": "deny"}}
        widened = {"edit": {"*": "deny", "/root/**": "allow"}}
        self.assertNotEqual(runtime.opencode_permission_ordered_view(restricted),
                            runtime.opencode_permission_ordered_view(widened))
        self.assertEqual(runtime.opencode_permission_effect(
            restricted, "edit", "/root/private/file"), "deny")
        self.assertEqual(runtime.opencode_permission_effect(
            widened, "edit", "/root/private/file"), "allow")
        same_allow = {"edit": {"*": "deny", "/root/**": "allow",
                               "/root/private/**": "allow"}}
        self.assertEqual(runtime.opencode_permission_ordered_view(same_allow),
                         runtime.opencode_permission_ordered_view(widened))

    def test_comparison_pruning_never_merges_different_last_match_results(self):
        import itertools
        rules = (("*", "deny"), ("/root/*", "allow"),
                 ("/root/private/*", "deny"), ("/elsewhere", "deny"))
        observations = {}
        for ordered in itertools.permutations(rules):
            rows = [{"action": "edit", "resource": resource, "effect": effect}
                    for resource, effect in ordered]
            expected = tuple(self.reference_effect(rows, "edit", resource) for resource in
                             ("", "/root/file", "/root/private/file", "/elsewhere", "/other"))
            view = runtime.opencode_permission_ordered_view({"edit": dict(ordered)})
            key = json.dumps(view, sort_keys=True)
            if key in observations:
                self.assertEqual(observations[key], expected, ordered)
            observations[key] = expected

    def test_interleaved_action_wildcards_and_repeated_resources_keep_last_match(self):
        rows = [
            {"action": "edit", "resource": "*", "effect": "deny"},
            {"action": "*", "resource": "/approved/**", "effect": "allow"},
            {"action": "e*", "resource": "/approved/**", "effect": "deny"},
            {"action": "edit", "resource": "*", "effect": "allow"},
        ]
        self.check_projection(rows, ("edit", "read", "execute"),
                              ("/approved/file", "/other/file", "*"))
        repeated = [
            {"action": "edit", "resource": "*", "effect": "deny"},
            {"action": "edit", "resource": "/approved/**", "effect": "allow"},
            {"action": "edit", "resource": "*", "effect": "deny"},
        ]
        projected = runtime.opencode_v2_to_legacy_config({"permissions": repeated})
        self.assertEqual(runtime.opencode_permission_rules_for_action(
            projected["permission"], "edit"),
            [("/approved/**", "allow"), ("*", "deny")])
        self.check_projection(repeated, ("edit",), ("/approved/file", "/other/file"))

    def test_resource_overlaps_selected_agent_and_native_v2_shape(self):
        source = {
            "theme": "system",
            "permissions": [
                {"action": "edit", "resource": "*", "effect": "deny"},
                {"action": "edit", "resource": "/approved/**", "effect": "allow"},
                {"action": "edit", "resource": "/approved/private/**", "effect": "deny"},
            ],
            "agents": {"build": {"description": "kept", "permissions": [
                {"action": "edit", "resource": "*", "effect": "allow"},
                {"action": "edit", "resource": "/approved/private/**", "effect": "deny"},
            ]}, "research": {"mode": "subagent", "permissions": []}},
        }
        original = json.loads(json.dumps(source))
        legacy = runtime.opencode_v2_to_legacy_config(source)
        self.assertEqual(source, original)
        self.assertEqual(legacy["agent"]["build"]["description"], "kept")
        self.assertEqual(legacy["agent"]["research"]["mode"], "subagent")
        self.assertEqual(runtime.opencode_permission_ordered_view(
            legacy["permission"]), runtime.opencode_permission_ordered_view(
                runtime.opencode_permission_from_config(source)))
        encoded = runtime.opencode_legacy_to_v2_config(legacy, original_v2=source)
        self.assertIn("permissions", encoded)
        self.assertIn("agents", encoded)
        self.assertNotIn("permission", encoded)
        self.assertNotIn("agent", encoded)
        self.assertEqual(encoded["theme"], "system")
        self.assertEqual(runtime.opencode_permission_ordered_view(
            runtime.opencode_permission_from_config(encoded)),
            runtime.opencode_permission_ordered_view(legacy["permission"]))
        self.check_projection(source["permissions"], ("edit",),
                              ("/approved/file", "/approved/private/file", "/other"))

    def test_malformed_or_empty_v2_permissions_do_not_become_allow_rows(self):
        for malformed in (None, {}, [{"action": "edit", "resource": "*", "effect": True}],
                          [{"action": "edit", "resource": "*", "effect": "allow",
                            "extra": "discarded"}]):
            with self.subTest(malformed=malformed), self.assertRaises(ValueError):
                runtime.opencode_v2_to_legacy_config({"permissions": malformed})
        for missing in (None, {}):
            self.assertEqual(runtime.opencode_permission_ordered_view(missing), [])
        with self.assertRaises(ValueError):
            runtime.opencode_permission_ordered_view([])

    def test_v2_action_pattern_that_collides_with_a_legacy_alias_fails_clearly(self):
        config = {"permissions": [
            {"action": "b*", "resource": "*", "effect": "allow"},
        ]}
        with self.assertRaisesRegex(ValueError, "conflicts with the V1 'bash' alias"):
            runtime.opencode_v2_to_legacy_config(config)


class OpenCodeCredentialReadTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / "opencode.db"
        with sqlite3.connect(self.db) as con:
            con.execute("CREATE TABLE credential (id TEXT PRIMARY KEY, integration_id TEXT, "
                        "label TEXT, value TEXT, active INTEGER, time_created INTEGER, time_updated INTEGER)")

    def _insert(self, con, identifier, integration, value, active, created):
        con.execute("INSERT INTO credential VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (identifier, integration, "label", json.dumps(value), active, created, created))

    def test_active_row_wins_and_reader_does_not_mutate_database(self):
        with sqlite3.connect(self.db) as con:
            self._insert(con, "old", "opencode-go", {"type": "key", "key": "old-secret"}, 0, 1)
            self._insert(con, "active", "opencode-go", {"type": "key", "key": "selected-secret"}, 1, 2)
        before = self.db.read_bytes()
        row = runtime.read_credential("opencode-go", db_path=self.db)
        self.assertEqual(row["credential_id"], "active")
        self.assertEqual(row["value"]["key"], "selected-secret")
        self.assertEqual(self.db.read_bytes(), before)
        self.assertTrue(runtime.credential_store_present(db_path=self.db))

    def test_null_migration_rows_select_newest_only_when_no_flagged_rows(self):
        with sqlite3.connect(self.db) as con:
            self._insert(con, "legacy-old", "opencode", {"type": "oauth", "access": "old"}, None, 1)
            self._insert(con, "legacy-new", "opencode", {"type": "oauth", "access": "new"}, None, 2)
        self.assertEqual(runtime.read_credential("opencode", db_path=self.db)["credential_id"],
                         "legacy-new")
        with sqlite3.connect(self.db) as con:
            self._insert(con, "inactive", "opencode", {"type": "oauth", "access": "inactive"}, 0, 3)
        self.assertIsNone(runtime.read_credential("opencode", db_path=self.db))

    def test_malformed_values_and_missing_integrations_are_unknown(self):
        with sqlite3.connect(self.db) as con:
            con.execute("INSERT INTO credential VALUES (?, ?, ?, ?, ?, ?, ?)",
                        ("bad", "openai", "label", "not-json", 1, 1, 1))
        row = runtime.read_credential("openai", db_path=self.db)
        self.assertEqual(row["credential_id"], "bad")
        self.assertIsNone(row["value"])
        self.assertFalse(row["credential_valid"])
        self.assertIsNone(runtime.read_credential("missing", db_path=self.db))

    def test_observation_distinguishes_missing_store_missing_row_and_bad_schema(self):
        missing = runtime.read_credential_observation(
            "opencode", db_path=self.db.with_name("not-created.db"))
        self.assertEqual(missing.state, "absent")
        self.assertEqual(runtime.read_credential_observation(
            "opencode", db_path=self.db).state, "absent")
        broken_link = Path(self.temp.name) / "broken-link.db"
        broken_link.symlink_to(Path(self.temp.name) / "missing-target.db")
        self.assertEqual(runtime.read_credential_observation(
            "opencode", db_path=broken_link).state, "unavailable")

        malformed = Path(self.temp.name) / "malformed.db"
        with sqlite3.connect(malformed) as con:
            con.execute("CREATE TABLE credential (id TEXT, integration_id TEXT)")
        observation = runtime.read_credential_observation("opencode", db_path=malformed)
        self.assertEqual(observation.state, "unavailable")
        self.assertEqual(observation.reason, "credential-store-schema-unavailable")
        # Keep the public boundary compatible while internal callers retain status.
        self.assertIsNone(runtime.read_credential("opencode", db_path=malformed))

    def test_selected_query_failure_and_ambiguous_active_rows_are_unavailable(self):
        with sqlite3.connect(self.db) as con:
            self._insert(con, "default-a", "opencode",
                         {"type": "oauth", "access": "a"}, 1, 2)
            self._insert(con, "default-b", "opencode",
                         {"type": "oauth", "access": "b"}, 1, 3)
        ambiguous = runtime.read_credential_observation("opencode", db_path=self.db)
        self.assertEqual(ambiguous.state, "unavailable")
        self.assertEqual(ambiguous.reason, "credential-selection-ambiguous")

        real_connect = sqlite3.connect

        class FailSelectedQuery:
            def __init__(self, connection):
                object.__setattr__(self, "connection", connection)

            def __getattr__(self, key):
                return getattr(self.connection, key)

            def __setattr__(self, key, value):
                setattr(self.connection, key, value)

            def execute(self, sql, *args):
                if "FROM credential WHERE" in sql:
                    raise sqlite3.OperationalError("database is locked")
                return self.connection.execute(sql, *args)

        with unittest.mock.patch.object(
                runtime.sqlite3, "connect",
                side_effect=lambda *args, **kwargs:
                FailSelectedQuery(real_connect(*args, **kwargs))):
            failed_query = runtime.read_credential_observation("opencode", db_path=self.db)
        self.assertEqual(failed_query.state, "unavailable")
        self.assertEqual(failed_query.reason, "credential-store-read-failed")

    def test_locked_database_is_unavailable_not_absent(self):
        with sqlite3.connect(self.db, timeout=0.1) as con:
            self._insert(con, "default", "opencode",
                         {"type": "oauth", "access": "selected"}, 1, 2)
        locker = sqlite3.connect(self.db, timeout=0.1)
        self.addCleanup(locker.close)
        locker.execute("BEGIN EXCLUSIVE")
        observation = runtime.read_credential_observation("opencode", db_path=self.db)
        locker.rollback()
        self.assertEqual(observation.state, "unavailable")
        self.assertEqual(observation.reason, "credential-store-read-failed")


class OpenCodeSessionObservationTest(unittest.TestCase):
    def test_v2_native_cursor_distinguishes_same_id_same_ms_without_event_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "native.db"
            with sqlite3.connect(db) as con:
                con.execute("CREATE TABLE session_v2 (id TEXT, directory TEXT, time_updated INT)")
                con.execute("CREATE TABLE session_message (id TEXT, session_id TEXT, type TEXT, "
                            "seq INT, time_created INT, time_updated INT, data TEXT)")
                con.execute("CREATE TABLE event_sequence (aggregate_id TEXT PRIMARY KEY, seq INT, owner_id TEXT)")
                con.execute("INSERT INTO session_v2 VALUES ('ses_retry', '/work', 2000)")
                payload = json.dumps({"time": {"created": 1500, "completed": 2000},
                                      "tokens": {"total": 300000}})
                con.execute("INSERT INTO session_message VALUES ('same-id', 'ses_retry', 'assistant', 1, 1500, 2000, ?)",
                            (payload,))
                con.execute("INSERT INTO event_sequence VALUES ('ses_retry', 1, NULL)")
                con.execute("INSERT INTO event_sequence VALUES ('ses_other', 900, NULL)")
            before = db.read_bytes()
            first = runtime.read_session_observation("ses_retry", db_path=db)
            self.assertEqual(first["event_seq"], 1)
            self.assertEqual(db.read_bytes(), before)
            with sqlite3.connect(db) as con:
                # Native retry updates the same projection without a unique
                # timestamp or insertion seq. Its aggregate cursor still moves.
                con.execute("UPDATE event_sequence SET seq=3 WHERE aggregate_id='ses_retry'")
            before = db.read_bytes()
            retry = runtime.read_session_observation("ses_retry", db_path=db)
            self.assertEqual(retry["event_seq"], 3)
            self.assertEqual(retry["messages"], first["messages"])
            self.assertEqual(db.read_bytes(), before)
            with sqlite3.connect(db) as con:
                con.execute("DROP TABLE event_sequence")
            unsupported = runtime.read_session_observation("ses_retry", db_path=db)
            self.assertIsNone(unsupported["event_seq"])
            self.assertEqual(unsupported["messages"], first["messages"])

    def test_v2_projection_and_cursor_share_a_read_snapshot_during_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "native.db"
            with sqlite3.connect(db) as con:
                con.execute("PRAGMA journal_mode=WAL")
                con.execute("CREATE TABLE session_v2 (id TEXT, directory TEXT, time_updated INT)")
                con.execute("CREATE TABLE session_message (id TEXT, session_id TEXT, type TEXT, "
                            "seq INT, time_created INT, time_updated INT, data TEXT)")
                con.execute("CREATE TABLE event_sequence (aggregate_id TEXT PRIMARY KEY, seq INT, owner_id TEXT)")
                con.execute("INSERT INTO session_v2 VALUES ('ses_retry', '/work', 2000)")
                con.execute("INSERT INTO session_message VALUES ('same-id', 'ses_retry', 'assistant', 1, 1500, 2000, ?)",
                            (json.dumps({"time": {"created": 1500}, "tokens": {"total": 100}}),))
                con.execute("INSERT INTO event_sequence VALUES ('ses_retry', 1, NULL)")
            read_messages = runtime._read_v2_messages
            def retry_during_read(*args, **kwargs):
                result = read_messages(*args, **kwargs)
                with sqlite3.connect(db) as writer:
                    writer.execute("UPDATE event_sequence SET seq=3 WHERE aggregate_id='ses_retry'")
                    writer.execute("UPDATE session_message SET data=? WHERE id='same-id'",
                                   (json.dumps({"time": {"created": 1500}, "tokens": {"total": 300000}}),))
                return result
            with unittest.mock.patch.object(runtime, "_read_v2_messages", side_effect=retry_during_read):
                observed = runtime.read_session_observation("ses_retry", db_path=db)
            self.assertEqual((observed["event_seq"], observed["messages"][0]["tokens"]["total"]), (1, 100))
            current = runtime.read_session_observation("ses_retry", db_path=db)
            self.assertEqual((current["event_seq"], current["messages"][0]["tokens"]["total"]), (3, 300000))

    def test_v2_exact_session_and_sequence_reader_needs_no_session_input_table(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "opencode.db"
            session_id = "ses_v2fixture"
            with sqlite3.connect(db) as con:
                con.execute("CREATE TABLE session_v2 (id TEXT PRIMARY KEY, directory TEXT, "
                            "parent_id TEXT, slug TEXT, title TEXT, agent TEXT, model TEXT, "
                            "time_created INTEGER, time_updated INTEGER, cost REAL, "
                            "tokens_input INTEGER, tokens_output INTEGER, tokens_reasoning INTEGER)")
                con.execute("CREATE TABLE session_message (id TEXT PRIMARY KEY, session_id TEXT, "
                            "type TEXT, seq INTEGER, time_created INTEGER, time_updated INTEGER, data TEXT)")
                con.execute("INSERT INTO session_v2 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            (session_id, "/work/tree", None, "slug", "worker", "build",
                             json.dumps({"id": "beam-free", "providerID": "opencode"}),
                             1000, 2000, 0.1, 7, 3, 0))
                con.execute("INSERT INTO session_message VALUES (?, ?, ?, ?, ?, ?, ?)",
                            ("msg_user", session_id, "user", 1, 1000, 1000,
                             json.dumps({"text": "question", "time": {"created": 1000}})))
                con.execute("INSERT INTO session_message VALUES (?, ?, ?, ?, ?, ?, ?)",
                            ("msg_assistant", session_id, "assistant", 2, 1500, 2000,
                             json.dumps({"agent": "build", "model": {"id": "beam-free", "providerID": "opencode"},
                                         "content": [{"type": "text", "text": "OK"}],
                                         "tokens": {"input": 7, "output": 1},
                                         "time": {"created": 1500, "completed": 2000}})))
            before = db.read_bytes()
            observation = runtime.read_session_observation(session_id, db_path=db)
            self.assertEqual(observation["state"], "observed")
            self.assertEqual(observation["generation"], "v2")
            self.assertEqual(observation["session"]["directory"], "/work/tree")
            self.assertEqual(observation["messages"][-1]["text"], "OK")
            self.assertEqual(observation["messages"][-1]["id"], "msg_assistant")
            self.assertEqual(observation["messages"][-1]["started"], 1500)
            self.assertEqual(observation["cursor_kind"], "session-message-seq-v2")
            delta = runtime.read_session_observation(session_id, db_path=db,
                                                    generation="v2", after_seq=2)
            self.assertEqual(delta["messages"], [])
            self.assertEqual(delta["cursor"], 2)
            self.assertEqual(db.read_bytes(), before)
            # Native retries reuse the row/id but update the projected start.
            with sqlite3.connect(db) as con:
                row = con.execute("SELECT data FROM session_message WHERE id='msg_assistant'").fetchone()
                payload = json.loads(row[0])
                payload["time"]["created"] = 1900
                con.execute("UPDATE session_message SET data=? WHERE id='msg_assistant'", (json.dumps(payload),))
            retried = runtime.read_session_observation(session_id, db_path=db)["messages"][-1]
            self.assertEqual((retried["id"], retried["created"], retried["started"]),
                             ("msg_assistant", 1500, 1900))

    def test_v2_shell_cursor_pages_all_row_types_and_refreshes_old_pending_seq(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "opencode.db"
            session_id = "ses_shellpagefixture"
            with sqlite3.connect(db) as con:
                con.execute("CREATE TABLE session_v2 (id TEXT PRIMARY KEY, directory TEXT, "
                            "time_created INTEGER, time_updated INTEGER)")
                con.execute("CREATE TABLE session_message (id TEXT PRIMARY KEY, session_id TEXT, "
                            "type TEXT, seq INTEGER, time_created INTEGER, time_updated INTEGER, data TEXT)")
                con.execute("INSERT INTO session_v2 VALUES (?, ?, ?, ?)",
                            (session_id, tmp, 1000, 1000))
                for seq in range(1, 71):
                    if seq == 1:
                        kind = "shell"
                        payload = {"metadata": {"background": True},
                                   "time": {"created": 1792456543124},
                                   "shellID": "sh_pending_old_window", "command": "sleep 20",
                                   "status": "running"}
                    else:
                        kind = "user" if seq % 2 else "assistant"
                        payload = {"text": "ordinary non-candidate row"}
                    con.execute("INSERT INTO session_message VALUES (?, ?, ?, ?, ?, ?, ?)",
                                ("msg-%d" % seq, session_id, kind, seq, seq, seq,
                                 json.dumps(payload)))

            first = runtime.read_session_observation(
                session_id, db_path=db, generation="v2", shell_after_seq=0)
            self.assertEqual(first["shell_cursor"]["next_seq"], runtime.MAX_SHELL_TASK_PAGE)
            self.assertFalse(first["shell_cursor"]["caught_up"])
            self.assertEqual(first["shell_cursor"]["mutable_seqs"], [1])
            self.assertEqual(first["shell_tasks"][0]["native_record_kind"],
                             "session-message-shell")

            completed = {"metadata": {"background": True},
                         "time": {"created": 1792456543124, "completed": 1792456547139},
                         "shellID": "sh_pending_old_window", "command": "sleep 20",
                         "status": "exited", "exit": 3,
                         "output": {"output": "private output omitted", "cursor": 17,
                                    "size": 17, "truncated": False}}
            with sqlite3.connect(db) as con:
                con.execute("UPDATE session_message SET data=?, time_updated=? "
                            "WHERE session_id=? AND seq=1",
                            (json.dumps(completed), 1792456547139, session_id))

            second = runtime.read_session_observation(
                session_id, db_path=db, generation="v2",
                shell_after_seq=first["shell_cursor"]["next_seq"],
                shell_refresh_seqs=first["shell_cursor"]["mutable_seqs"])
            cursor = second["shell_cursor"]
            self.assertEqual(cursor["next_seq"], 70)
            self.assertTrue(cursor["caught_up"])
            self.assertEqual(cursor["refreshed_seqs"], [1])
            task = second["shell_tasks"][0]
            self.assertEqual(task["native_status"], "exited")
            self.assertEqual(task["exit_code"], 3)
            self.assertEqual(task["completed_at_ms"], 1792456547139)
            self.assertEqual(task["native_output_cursor"], 17)
            self.assertEqual(task["native_output_size"], 17)
            self.assertFalse(task["native_output_truncated"])
            self.assertNotIn("private output omitted", repr(task))

    def test_same_sid_in_both_generations_is_unknown_without_runtime_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "opencode.db"
            with sqlite3.connect(db) as con:
                con.execute("CREATE TABLE session (id TEXT PRIMARY KEY, directory TEXT, time_updated INTEGER)")
                con.execute("CREATE TABLE session_v2 (id TEXT PRIMARY KEY, directory TEXT, time_updated INTEGER)")
                con.execute("INSERT INTO session VALUES ('ses_dupe', '/old', 1)")
                con.execute("INSERT INTO session_v2 VALUES ('ses_dupe', '/new', 2)")
            self.assertEqual(runtime.read_session_observation("ses_dupe", db_path=db)["reason"],
                             "session-generation-ambiguous")
            self.assertEqual(runtime.read_session_observation("ses_dupe", db_path=db,
                                                              generation="v2")["session"]["directory"],
                             "/new")


class OpenCodeProviderEvidenceTest(unittest.TestCase):
    def test_unavailable_credential_scope_cannot_read_or_record_model_rejection(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "opencode.db"
            with sqlite3.connect(db) as con:
                con.execute("CREATE TABLE credential (id TEXT, integration_id TEXT)")
            tools = str(ROOT / "tools")
            if tools not in sys.path:
                sys.path.insert(0, tools)
            from fleet.collectors import usage_cache
            with unittest.mock.patch.dict(os.environ, {
                    "OPENCODE_RUNTIME_VERSION": "2.0.32", "OPENCODE_DB": str(db)}, clear=True), \
                 unittest.mock.patch.object(usage_cache, "record_provider") as record:
                self.assertEqual(runtime.model_availability("opencode-go/model")["state"],
                                 "unknown")
                self.assertFalse(runtime.record_model_outcome(
                    "opencode-go/model", error={"status": 401}))
            record.assert_not_called()

    def test_only_typed_expiry_and_entitlement_errors_are_hard(self):
        expired = runtime.classify_usage_rejection(
            401, {"error": {"code": "api_key_expired", "message": "expired"}})
        self.assertEqual(expired["reason"], "credential-expired")
        missing = runtime.classify_usage_rejection(
            403, {"error": {"type": "EntitlementError",
                            "message": "OpenCode Go subscription required."}})
        self.assertEqual(missing["reason"], "subscription-missing")
        self.assertIsNone(runtime.classify_usage_rejection(403, {"message": "cloudflare 1010"}))
        self.assertEqual(runtime.classify_usage_rejection(401, {})["reason"],
                         "authentication-rejected")

    def test_provider_errors_are_redacted_for_terminal_receipts(self):
        result = runtime.safe_provider_error({"type": "provider.auth", "status": 401,
                                              "code": "api_key_expired",
                                              "message": "Bearer secret-token"})
        self.assertNotIn("secret-token", json.dumps(result))
        self.assertEqual(result["status"], 401)

    def test_configured_fallbacks_are_source_owned(self):
        fallbacks = runtime.automatic_model_fallbacks()
        self.assertIn(("openai/gpt-6.1-sol", "runtime-default"), fallbacks)
        self.assertNotIn(("opencode/beam-free", "runtime-default"), fallbacks)


# The exact error OpenCode 2.0.32 returned to the replacement owner that died
# in 2.8 seconds (2026-10-20): typed, with no numeric status.
INCIDENT_ERROR = {"type": "provider.auth", "message": "Request failed: 401"}


class OpenCodeTypedErrorTest(unittest.TestCase):
    def test_typed_auth_is_confirmed_without_a_numeric_status(self):
        self.assertEqual(runtime.classify_model_error(INCIDENT_ERROR),
                         {"state": "unavailable", "reason": "authentication-rejected"})
        self.assertEqual(runtime.classify_model_error(
            {"type": "provider.auth", "code": "api_key_expired"})["reason"], "credential-expired")

    def test_transient_types_and_bare_messages_stay_unknown(self):
        for kind, reason in runtime.TYPED_TRANSIENT_ERRORS.items():
            with self.subTest(kind=kind):
                # A 401 in the text does not turn a typed transient into auth.
                self.assertEqual(runtime.classify_model_error(
                    {"type": kind, "message": "Request failed: 401"}),
                    {"state": "unknown", "reason": reason})
        for error in ({"message": "Request failed: 401"}, {"status": True},
                      {"status": "401"}, {}, None):
            with self.subTest(error=error):
                self.assertEqual(runtime.classify_model_error(error)["state"], "unknown")
        self.assertEqual(runtime.classify_model_error({"status": 429}),
                         {"state": "unknown", "reason": "rate-limited"})

    def test_numeric_statuses_keep_their_existing_meaning(self):
        self.assertEqual(runtime.classify_model_error({"status": 401})["reason"],
                         "authentication-rejected")
        self.assertEqual(runtime.classify_model_error(
            {"status": 401, "code": "api_key_expired"})["reason"], "credential-expired")
        self.assertEqual(runtime.classify_model_error(
            {"status": 403, "type": "EntitlementError",
             "message": "OpenCode Go subscription required."})["reason"], "subscription-missing")


class OpenCodeAutomaticEligibilityTest(unittest.TestCase):
    """Provider evidence against a real V2 credential store and Fleet cache."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.db = self.root / "opencode.db"
        self.state = self.root / "state"
        with sqlite3.connect(self.db) as con:
            con.execute("CREATE TABLE credential (id TEXT PRIMARY KEY, integration_id TEXT, "
                        "label TEXT, value TEXT, active INTEGER, time_created INTEGER, "
                        "time_updated INTEGER)")
        self.put("openai-oauth", "openai", {"type": "oauth", "access": "openai-access-1",
                                            "refresh": "openai-refresh-1", "expires": 1})
        self.put("go-oauth", "opencode", {"type": "oauth", "access": "go-access-1"})
        env = unittest.mock.patch.dict(os.environ, {
            "HOME": str(self.root), "XDG_STATE_HOME": str(self.state),
            "XDG_DATA_HOME": str(self.root / "data"), "OPENCODE_DB": str(self.db),
            "OPENCODE_RUNTIME_VERSION": "2.0.32", "PATH": os.environ.get("PATH", "")},
            clear=True)
        env.start()
        self.addCleanup(env.stop)
        self.config = self.root / "models.conf"
        self.fallbacks()

    def put(self, identifier, integration, value, label="Default"):
        with sqlite3.connect(self.db) as con:
            con.execute("INSERT OR REPLACE INTO credential VALUES (?, ?, ?, ?, 1, 1, 2)",
                        (identifier, integration, label, json.dumps(value)))

    def fallbacks(self, *items):
        self.config.write_text('CFG_V2_AUTO_FALLBACKS="%s"\n' % " ".join(items))

    def cache_text(self):
        path = self.state / "agent-fleet" / "usage" / "opencode.json"
        return path.read_text() if path.exists() else ""

    def rows(self, provider):
        text = self.cache_text()
        providers = json.loads(text).get("providers", {}) if text else {}
        return {row["model"]: row for row in providers.values() if row["provider"] == provider}

    def test_incident_error_is_recorded_for_the_openai_account_without_secrets(self):
        self.assertTrue(runtime.record_model_outcome("openai/gpt-6.1-sol", error=INCIDENT_ERROR))
        rows = self.rows("openai")
        self.assertEqual(set(rows), {"gpt-6.1-sol", "*"})
        for row in rows.values():
            self.assertEqual((row["state"], row["reason"]),
                             ("unavailable", "authentication-rejected"))
            self.assertEqual(row["account_scope"], runtime.account_scope("openai"))
            self.assertTrue(row["credential_generation"].startswith("gen:"))
        for secret in ("openai-access-1", "openai-refresh-1", "go-access-1"):
            self.assertNotIn(secret, self.cache_text())
        evidence = runtime.model_availability("openai/gpt-6.1-sol")
        self.assertEqual((evidence["state"], evidence["automatic_eligible"]), ("unavailable", False))
        self.assertNotIn("credential_generation", evidence)
        # Go is another provider on the same runtime; its account is untouched.
        self.assertEqual(self.rows("opencode-go"), {})
        self.assertTrue(runtime.model_availability("opencode-go/model")["automatic_eligible"])

    def test_changed_credential_releases_the_rejection_and_a_label_change_does_not(self):
        runtime.record_model_outcome("openai/gpt-6.1-sol", error=INCIDENT_ERROR)
        self.put("openai-oauth", "openai", {"type": "oauth", "access": "openai-access-1",
                                            "refresh": "openai-refresh-1", "expires": 1},
                 label="Renamed")
        self.assertEqual(runtime.model_availability("openai/gpt-6.1-sol")["state"], "unavailable")
        # Re-login overwrites the same row id: same account scope, new credential.
        self.put("openai-oauth", "openai", {"type": "oauth", "access": "openai-access-2",
                                            "refresh": "openai-refresh-2", "expires": 2})
        evidence = runtime.model_availability("openai/gpt-6.1-sol")
        self.assertEqual(evidence["state"], "unknown")
        self.assertEqual(evidence["observation_reason"], "credential-generation-changed")
        self.assertFalse(evidence["automatic_eligible"])

    def test_a_rejection_without_a_credential_generation_is_not_persisted(self):
        # Nothing could release it after a re-login, so it stays unknown.
        unnamed = runtime.AccountScopeObservation("present", "account:unnamed")
        self.assertFalse(runtime.record_model_outcome(
            "openai/gpt-6.1-sol", error=INCIDENT_ERROR, account_observation=unnamed))
        with sqlite3.connect(self.db) as con:
            con.execute("DELETE FROM credential WHERE integration_id='openai'")
        self.assertEqual(runtime.model_account_observation("openai/gpt-6.1-sol").scope, "unscoped")
        self.assertFalse(runtime.record_model_outcome("openai/gpt-6.1-sol", error=INCIDENT_ERROR))
        self.assertEqual(self.rows("openai"), {})
        # A success still records, as before.
        self.assertTrue(runtime.record_model_outcome(
            "openai/gpt-6.1-sol", success=True, account_observation=unnamed))

    def test_generation_follows_every_credential_field_but_not_the_label(self):
        def generation(**value):
            return runtime._credential_generation(
                {"integration_id": "openai", "label": "x", "source": "opencode-sqlite",
                 "value": {"type": "oauth", "access": "a", "refresh": "r", **value}})
        self.assertNotEqual(generation(expires=1), generation(expires=2))
        self.assertNotEqual(generation(), generation(access="b"))
        self.assertEqual(generation(expires=1), generation(expires=1))
        self.assertIsNone(runtime._credential_generation(
            {"integration_id": "openai", "value": {"type": "oauth", "expires": 1}}))

    def test_outcome_is_credited_to_the_account_the_request_started_with(self):
        started = runtime.model_account_observation("openai/gpt-6.1-sol")
        self.put("openai-oauth", "openai", {"type": "oauth", "access": "openai-access-2",
                                            "refresh": "openai-refresh-2"})
        runtime.record_model_outcome("openai/gpt-6.1-sol", error=INCIDENT_ERROR,
                                     account_observation=started)
        self.assertEqual(self.rows("openai")["gpt-6.1-sol"]["state"], "unavailable")
        self.assertEqual(runtime.model_availability("openai/gpt-6.1-sol")["observation_reason"],
                         "credential-generation-changed")

    def test_transient_errors_neither_persist_nor_clear_a_rejection(self):
        for kind in runtime.TYPED_TRANSIENT_ERRORS:
            self.assertFalse(runtime.record_model_outcome("openai/gpt-6.1-sol",
                                                          error={"type": kind}))
        self.assertEqual(self.cache_text(), "")
        runtime.record_model_outcome("openai/gpt-6.1-sol", error=INCIDENT_ERROR)
        for kind in runtime.TYPED_TRANSIENT_ERRORS:
            runtime.record_model_outcome("openai/gpt-6.1-sol", error={"type": kind})
        self.assertEqual(runtime.record_model_http_result("openai/gpt-6.1-sol", 429),
                         {"state": "unknown", "reason": "rate-limited", "http_status": 429})
        self.assertEqual(runtime.model_availability("openai/gpt-6.1-sol")["reason"],
                         "authentication-rejected")

    def test_only_a_probed_provider_unknown_is_automatic(self):
        openai = runtime.model_availability("openai/gpt-6.1-sol")
        self.assertEqual((openai["state"], openai["reason"], openai["automatic_eligible"]),
                         ("unknown", "provider-model-unverified", False))
        self.assertEqual(openai["observation_reason"], "no-observation")
        go = runtime.model_availability("opencode-go/model")
        self.assertEqual((go["state"], go["automatic_eligible"]), ("unknown", True))
        self.assertFalse(runtime.automatic_eligible("not-a-model", {"state": "unknown"}))
        self.assertFalse(runtime.automatic_eligible("opencode-go/model", {"state": "unavailable"}))

    def test_account_success_admits_only_its_model_for_an_unprobed_provider(self):
        self.assertTrue(runtime.record_model_outcome("openai/model-a", success=True))
        self.assertTrue(runtime.model_availability("openai/model-a")["automatic_eligible"])
        self.assertFalse(runtime.model_availability("openai/model-b")["automatic_eligible"])
        runtime.record_model_outcome("opencode-go/model-a", success=True)
        self.assertEqual(runtime.model_availability("opencode-go/model-b")["state"], "usable")

    def test_selection_prefers_an_eligible_candidate_and_never_trades_a_rejection(self):
        select = lambda model: runtime.select_automatic_model(
            model, "xhigh", config_path=self.config)
        # Unverified primary, verifiable fallback: the fallback runs.
        self.fallbacks("opencode-go/fallback:max")
        model, variant, evidence = select("openai/gpt-6.1-sol")
        self.assertEqual((model, variant), ("opencode-go/fallback", "max"))
        self.assertEqual(evidence["fallback_from_reason"], "provider-model-unverified")
        # Nothing eligible: the unverified primary is returned, marked ineligible.
        self.fallbacks("openai/gpt-6.1-sol:runtime-default")
        model, variant, evidence = select("openai/gpt-6.1-sol")
        self.assertEqual((model, variant, evidence["state"], evidence["automatic_eligible"]),
                         ("openai/gpt-6.1-sol", "xhigh", "unknown", False))
        # A confirmed rejection is not traded for an unverified fallback.
        runtime.record_model_outcome("opencode-go/primary", error={"type": "provider.auth"})
        model, _variant, evidence = select("opencode-go/primary")
        self.assertEqual((model, evidence["state"]), ("opencode-go/primary", "unavailable"))
        # An eligible primary stays first.
        self.assertEqual(select("opencode-go/other")[0], "opencode-go/other")

    def test_candidates_keep_their_requested_model_identity(self):
        runtime.record_model_outcome("opencode-go/model", success=True)
        rows = runtime.automatic_candidates("opencode-go/unseen", "high",
                                            config_path=self.config)
        self.assertEqual(rows[0]["model"], "opencode-go/unseen")
        self.assertEqual(rows[0]["evidence_model"], "*")


if __name__ == "__main__":
    unittest.main()
