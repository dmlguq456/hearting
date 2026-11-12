#!/usr/bin/env python3
"""T4 hook-chain measurement: selection, isolation contract and violation detection.

Nothing here measures real hooks or builds a production-size fixture; every
case uses a throw-away code root, state root and operational root in a temp dir.
There are no timing assertions.
"""
import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


mh = _load("measure_hook_before_after", ROOT / "tools" / "measure_hook_before_after.py")
t4 = _load("e2e_t4", HERE / "t4.py")

# sha256 of the pre-existing read-only ``_SHIM`` (the before/after method must not change).
LEGACY_SHIM_SHA256 = "605c5ae373b2838c9be0d11373c5cf8670092e1354f399cc83e43e477ed1b770"
GIT_ENV = {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}


def _git(*args, cwd):
    subprocess.run(["git", "-C", str(cwd), "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                    *args], check=True, capture_output=True, env=dict(os.environ, **GIT_ENV))


def _settings(code_root: Path, harness: str, groups: dict) -> None:
    """Write a fake settings/hooks file with ``groups`` = {event: [(matcher, command, extra), ...]}."""
    path = code_root / mh.CHAIN_SETTINGS[harness]
    path.parent.mkdir(parents=True, exist_ok=True)
    hooks = {}
    for event, entries in groups.items():
        hooks[event] = [{"matcher": matcher, "hooks": [dict({"type": "command", "command": command},
                                                            **extra)]}
                        for matcher, command, extra in entries]
    path.write_text(json.dumps({"hooks": hooks}), encoding="utf-8")


class Rig:
    """A fake code root (git repo), state root and operational root under one temp dir."""

    def __init__(self, testcase: unittest.TestCase):
        tmp = tempfile.TemporaryDirectory(prefix="t4-test-")
        testcase.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.code = self.tmp / "code"
        self.ops = self.tmp / "ops"
        self.state = self.tmp / "hearting-e2e-t4test"
        self.outside = self.tmp / "outside"
        for path in (self.code, self.ops, self.state, self.outside):
            path.mkdir()
        (self.code / "tracked.txt").write_text("one\n", encoding="utf-8")
        self.marker = "hearting-e2e-t4test"

    def commit(self) -> None:
        _git("init", "-q", cwd=self.code)
        _git("add", "-A", cwd=self.code)
        _git("commit", "-q", "-m", "init", cwd=self.code)

    def measure(self, harness="claude", event="prompt"):
        return mh.measure_chain([f"{harness}:{event}"], self.code, self.state, repeat=1,
                                ops_roots=[self.ops], marker=self.marker, settle_s=0)


class SelectionTests(unittest.TestCase):
    def test_matcher_and_events_for_claude_tool(self):
        with tempfile.TemporaryDirectory() as tmp:
            code = Path(tmp)
            _settings(code, "claude", {
                "UserPromptSubmit": [(None, "p1", {}), ("*", "p2", {"async": True})],
                "PreToolUse": [("Edit|Write", "skip-pre", {}), ("Edit|Write|Bash", "pre", {})],
                "PostToolUse": [("*", "post-any", {}), ("Read", "skip-read", {}),
                                ("Bash", "post-bash", {"asyncRewake": True})],
                "Stop": [("*", "stop1", {})],
            })
            path = code / mh.CHAIN_SETTINGS["claude"]
            tool = mh.chain_commands(path, "claude", "tool")
            self.assertEqual([item["command"] for item in tool], ["pre", "post-any", "post-bash"])
            self.assertEqual([item["event"] for item in tool], ["PreToolUse", "PostToolUse", "PostToolUse"])
            self.assertTrue(tool[2]["async_rewake"])
            prompt = mh.chain_commands(path, "claude", "prompt")
            self.assertEqual([(item["command"], item["async"]) for item in prompt],
                             [("p1", False), ("p2", True)])
            self.assertEqual([item["command"] for item in mh.chain_commands(path, "claude", "stop")],
                             ["stop1"])

    def test_matcher_matches(self):
        self.assertTrue(mh.matcher_matches(None, "Bash"))
        self.assertTrue(mh.matcher_matches("*", "Bash"))
        self.assertTrue(mh.matcher_matches("Write|Bash|functions\\.exec_command", "Bash"))
        self.assertFalse(mh.matcher_matches("Write|Edit", "Bash"))
        self.assertFalse(mh.matcher_matches("Bas", "Bash"))

    def test_real_settings_resolve_to_existing_hook_files(self):
        for harness in mh.CHAIN_SETTINGS:
            for event in mh.CHAIN_EVENTS:
                entries = mh.chain_commands(ROOT / mh.CHAIN_SETTINGS[harness], harness, event)
                self.assertTrue(entries, f"{harness}:{event} selects no hook")
                for entry in entries:
                    script = mh.hook_script(entry["command"], harness)
                    self.assertIsNotNone(script, entry["command"][:80])
                    self.assertTrue((ROOT / script).is_file(), script)

    def test_hook_script_forms(self):
        self.assertEqual(mh.hook_script(
            'sh "${CLAUDE_CONFIG_DIR:-$HOME/.claude}/hooks/run-hook.sh" bash mem-recall-inject.sh', "claude"),
            "hooks/mem-recall-inject.sh")
        self.assertEqual(mh.hook_script(
            'sh "${CLAUDE_CONFIG_DIR:-$HOME/.claude}/hooks/run-hook.sh" python3 peer-message-record.py stop',
            "claude"), "hooks/peer-message-record.py")
        self.assertEqual(mh.hook_script(
            "sh -c 'exec \"$root/adapters/codex/hooks/run-hook.sh\" turn-carrier.py stop'", "codex"),
            "adapters/codex/hooks/turn-carrier.py")
        self.assertIsNone(mh.hook_script("echo hi", "claude"))


class PathGapTests(unittest.TestCase):
    def test_gap_names_from_fake_bodies(self):
        self.assertEqual(mh.path_gap_names('x = shutil.which("herdr")\n'), ["herdr"])
        self.assertEqual(mh.path_gap_names("import subprocess\nsubprocess.run(['tmux', 'ls'])\n"), ["tmux"])
        self.assertEqual(mh.path_gap_names("for h in ['claude', 'codex']:\n    pass\n"), [])
        self.assertEqual(mh.path_gap_names("if ! command -v opencode >/dev/null; then :; fi\n"), ["opencode"])
        self.assertEqual(mh.path_gap_names("herdr pane list\n", shell=True), ["herdr"])
        self.assertEqual(mh.path_gap_names("herdr pane list\n"), [])
        self.assertEqual(mh.env_gap_names('os.environ.get("HERDR_PANE_ID")'), ["env:HERDR_*"])
        self.assertEqual(mh.env_gap_names('[ -n "$TMUX_PANE" ]'), ["env:TMUX*"])

    def test_hook_path_gaps_reads_imported_utility(self):
        with tempfile.TemporaryDirectory() as tmp:
            code = Path(tmp)
            (code / "hooks").mkdir()
            (code / "utilities").mkdir()
            (code / "hooks" / "h.py").write_text("import helper_mod\nimport os\n", encoding="utf-8")
            (code / "utilities" / "helper_mod.py").write_text(
                'import shutil\nshutil.which("herdr")\nimport os\nos.environ.get("TMUX")\n',
                encoding="utf-8")
            self.assertEqual(mh.hook_path_gaps(code, "hooks/h.py"), ["env:TMUX*", "herdr"])
            self.assertEqual(mh.hook_path_gaps(code, "hooks/missing.py"), [])
            self.assertEqual(mh.hook_path_gaps(code, None), [])


class ShimTests(unittest.TestCase):
    def test_chain_shim_refuses_outside_writes_and_sockets(self):
        rig = Rig(self)
        shim = rig.tmp / "shim"
        shim.mkdir()
        (shim / "sitecustomize.py").write_text(mh._CHAIN_SHIM, encoding="utf-8")
        scratch = rig.state
        victim = rig.outside / "victim.txt"
        victim.write_text("keep", encoding="utf-8")
        (scratch / "inside").mkdir()
        script = "\n".join([
            "import os, socket, sys",
            "res = {}",
            "def attempt(name, fn):",
            "    try:",
            "        fn(); res[name] = 'ok'",
            "    except PermissionError:",
            "        res[name] = 'denied'",
            f"scratch = {str(scratch)!r}",
            f"outside = {str(rig.outside)!r}",
            "attempt('write-inside', lambda: open(os.path.join(scratch, 'inside', 'a'), 'w').close())",
            "attempt('write-outside', lambda: open(os.path.join(outside, 'new'), 'w').close())",
            "attempt('append-outside', lambda: open(os.path.join(outside, 'victim.txt'), 'a').close())",
            "attempt('read-outside', lambda: open(os.path.join(outside, 'victim.txt')).read())",
            "attempt('remove-outside', lambda: os.remove(os.path.join(outside, 'victim.txt')))",
            "attempt('rename-into-outside', lambda: os.rename(os.path.join(scratch, 'inside', 'a'),"
            " os.path.join(outside, 'moved')))",
            "attempt('mkdir-outside', lambda: os.mkdir(os.path.join(outside, 'd')))",
            "def mkdir_fd():",
            "    fd = os.open(outside, os.O_RDONLY)",
            "    try:",
            "        os.mkdir('viafd', dir_fd=fd)",
            "    finally:",
            "        os.close(fd)",
            "attempt('mkdir-dirfd-outside', mkdir_fd)",
            "def open_dirfd(directory, cwd):",
            "    os.chdir(cwd)",
            "    fd = os.open(directory, os.O_RDONLY)",
            "    try:",
            "        os.close(os.open('lockfile', os.O_WRONLY | os.O_CREAT, 0o600, dir_fd=fd))",
            "    finally:",
            "        os.close(fd); os.chdir(scratch)",
            # relative name + dir_fd inside the fixture is allowed even when the cwd is outside
            "attempt('open-dirfd-inside', lambda: open_dirfd(os.path.join(scratch, 'inside'), outside))",
            # and the same name with a dir_fd outside the fixture is refused even when the cwd is inside
            "attempt('open-dirfd-outside', lambda: open_dirfd(outside, scratch))",
            "def link_escape():",
            "    os.symlink(outside, os.path.join(scratch, 'escape'))",
            "    open(os.path.join(scratch, 'escape', 'viasymlink'), 'w').close()",
            "attempt('write-through-symlink', link_escape)",
            "attempt('devnull', lambda: open('/dev/null', 'w').close())",
            "def connect():",
            "    s = socket.socket(socket.AF_UNIX); s.connect(os.path.join(scratch, 'no.sock'))",
            "attempt('socket-connect', connect)",
            "print(repr(res))",
        ])
        env = {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(shim),
               "CHAIN_SCRATCH": str(scratch), "HOOK_AUDIT_LOG": str(scratch / "audit"),
               "PYTHONDONTWRITEBYTECODE": "1"}
        completed = subprocess.run([sys.executable, "-c", script], cwd=str(scratch), env=env,
                                   capture_output=True, text=True, timeout=60)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        res = eval(completed.stdout.strip().splitlines()[-1])  # noqa: S307 - our own repr of a dict
        self.assertEqual(res["write-inside"], "ok")
        self.assertEqual(res["read-outside"], "ok")
        self.assertEqual(res["devnull"], "ok")
        self.assertEqual(res["open-dirfd-inside"], "ok")
        self.assertTrue((scratch / "inside" / "lockfile").is_file())
        for name in ("write-outside", "append-outside", "remove-outside", "rename-into-outside",
                     "mkdir-outside", "mkdir-dirfd-outside", "open-dirfd-outside", "write-through-symlink",
                     "socket-connect"):
            self.assertEqual(res[name], "denied", name)
        self.assertEqual(victim.read_text(encoding="utf-8"), "keep")
        self.assertEqual(sorted(path.name for path in rig.outside.iterdir()), ["victim.txt"])  # no 'lockfile' either
        denied = mh._gather_audit(str(scratch / "audit"))
        self.assertTrue(any(item.startswith("socket.connect") for item in denied), denied)
        self.assertTrue(any(item.startswith("open-write:") for item in denied), denied)


class ViolationTests(unittest.TestCase):
    def _run(self, rig, commands, harness="claude"):
        event = "UserPromptSubmit"
        _settings(rig.code, harness, {event: [(None, command, {}) for command in commands]})
        rig.commit()
        return rig.measure(harness)

    def test_quiet_hook_has_no_violation(self):
        rig = Rig(self)
        result = self._run(rig, ["true"])
        self.assertEqual(result["violations"], [])
        self.assertFalse(result["discarded"])
        self.assertEqual(result["contract"], "python-deny+fingerprint")
        self.assertEqual(result["chains"]["claude:prompt"]["n_hooks"], 1)

    def test_shell_hook_writing_into_code_root_is_noticed(self):
        rig = Rig(self)
        result = self._run(rig, ["true", f"echo hi > '{rig.code}/leaked.txt'"])
        self.assertTrue(result["discarded"])
        self.assertEqual(result["chains"], {})
        self.assertEqual(len(result["violations"]), 1)
        violation = result["violations"][0]
        self.assertIn("leaked.txt", violation["hook"] + json.dumps(violation["code_root_changed"]))
        self.assertTrue(any("leaked.txt" in item for item in violation["code_root_changed"]))
        self.assertIn("echo hi", violation["hook"])

    def test_shell_hook_modifying_tracked_file_is_noticed(self):
        rig = Rig(self)
        result = self._run(rig, [f"echo more >> '{rig.code}/tracked.txt'"])
        self.assertTrue(result["discarded"])
        self.assertEqual(result["violations"][0]["code_root_changed"], ["tracked.txt"])

    def test_shell_hook_creating_marker_name_in_operational_root_is_noticed(self):
        rig = Rig(self)
        (rig.ops / "sub").mkdir()
        leak = rig.ops / "sub" / f"{rig.marker}-leak"
        result = self._run(rig, [f": > '{leak}'"])
        self.assertTrue(result["discarded"])
        violation = result["violations"][0]
        self.assertEqual(violation["operational_new_names"], [str(leak)])
        self.assertEqual(violation["code_root_changed"], [])
        # An unrelated new name in the operational root (another session) is not this run's write.
        rig2 = Rig(self)
        other = rig2.ops / "someone-elses-file"
        result2 = self._run(rig2, [f": > '{other}'"])
        self.assertEqual(result2["violations"], [])

    def test_python_hook_write_outside_is_refused_not_violation(self):
        rig = Rig(self)
        target = rig.outside / "py-wrote"
        command = f"python3 -c \"open('{target}', 'w').close()\""
        result = self._run(rig, [command])
        self.assertFalse(target.exists())
        self.assertEqual(result["violations"], [])
        hook = result["chains"]["claude:prompt"]["hooks"][0]
        self.assertTrue(any(item.startswith("open-write:") for item in hook["denied"]), hook["denied"])
        self.assertTrue(any("쓰기·socket 거부" in note and "하한" in note for note in result["notes"]))
        self.assertEqual(result["chains"]["claude:prompt"]["sum_s_lower_bound_hooks"],
                         round(hook["median_ms"] / 1000, 6))

    def test_cli_exit_codes_and_contract(self):
        rig = Rig(self)
        _settings(rig.code, "codex", {"Stop": [(None, "true", {})]})
        rig.commit()
        argv = ["--chain", "codex:stop", "--code-root", str(rig.code), "--state-root", str(rig.state),
                "--ops-root", str(rig.ops), "--marker", rig.marker, "--repeat", "1",
                "--json", str(rig.tmp / "out.json")]
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(mh.main(argv), 0)
        out = json.loads((rig.tmp / "out.json").read_text(encoding="utf-8"))
        self.assertEqual(out["contract"], "python-deny+fingerprint")
        self.assertIn("codex:stop", out["chains"])
        self.assertEqual(out["chains"]["codex:stop"]["sum_s"],
                         round(out["chains"]["codex:stop"]["hooks"][0]["median_ms"] / 1000, 6))
        _settings(rig.code, "codex", {"Stop": [(None, f"echo x > '{rig.code}/new.txt'", {})]})
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(mh.main(argv), 1)

    def test_state_root_inside_operational_root_is_refused(self):
        rig = Rig(self)
        _settings(rig.code, "claude", {"UserPromptSubmit": [(None, "true", {})]})
        rig.commit()
        with self.assertRaises(ValueError):
            mh.measure_chain(["claude:prompt"], rig.code, rig.state, repeat=1,
                             ops_roots=[rig.tmp], marker=rig.marker, settle_s=0)
        with self.assertRaises(ValueError):
            mh.measure_chain(["claude:bogus"], rig.code, rig.state, repeat=1,
                             ops_roots=[rig.ops], marker=rig.marker, settle_s=0)


class LegacyMethodTests(unittest.TestCase):
    def test_before_after_method_is_unchanged(self):
        self.assertEqual(hashlib.sha256(mh._SHIM.encode("utf-8")).hexdigest(), LEGACY_SHIM_SHA256)
        self.assertEqual(len(mh.EXCLUDED_PATHS), 6)
        help_text = io.StringIO()
        with contextlib.redirect_stdout(help_text), self.assertRaises(SystemExit):
            mh.main(["--help"])
        for flag in ("--before-code-root", "--before-state-root", "--after-code-root",
                     "--after-state-root", "--session", "--repeat", "--json", "--keep-scratch"):
            self.assertIn(flag, help_text.getvalue())
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors), self.assertRaises(SystemExit) as caught:
            mh.main([])
        self.assertEqual(caught.exception.code, 2)
        self.assertIn("the following arguments are required: --before-code-root, "
                      "--before-state-root, --after-code-root, --after-state-root", errors.getvalue())

    def test_before_after_output_keys_with_fake_code_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            code = tmp / "code"
            (code / "hooks").mkdir(parents=True)
            (code / "utilities").mkdir()
            (code / "hooks" / "dispatch-session-sweep.py").write_text("print('{}')\n", encoding="utf-8")
            (code / "utilities" / "dispatch_session_sweep.py").write_text(
                "def addressed_records(root, kind, session):\n    return [], 0\n", encoding="utf-8")
            state = tmp / "state"
            (state / "hearting" / "dispatch").mkdir(parents=True)
            out = tmp / "out.json"
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(mh.main([
                    "--before-code-root", str(code), "--before-state-root", str(state),
                    "--after-code-root", str(code), "--after-state-root", str(state),
                    "--repeat", "2", "--json", str(out)]), 0)
            result = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual(sorted(result), [
                "after", "before", "excluded_paths", "input_digests_match", "measured_at_utc",
                "read_only_lower_bound", "same_input_state", "schema", "session"])
            self.assertTrue(result["read_only_lower_bound"])
            self.assertNotIn("contract", result)
            self.assertEqual(sorted(result["before"]), [
                "code_root", "discovery", "discovery_median_ns", "entrypoint_median_ns",
                "entrypoint_runs", "primed_state_digest", "priming", "state_digest", "state_root"])


class T4ModuleTests(unittest.TestCase):
    def test_percentile_and_stats(self):
        self.assertEqual(t4.percentile([0.3, 0.1, 0.2], 0.95), 0.3)
        self.assertEqual(t4.percentile(list(range(1, 21)), 0.95), 19)
        stats = t4.event_stats([0.2, 0.1, 0.3])
        self.assertEqual((stats["n"], stats["median_s"], stats["p95_s"], stats["max_s"]), (3, 0.2, 0.3, 0.3))
        self.assertEqual(stats["p95_basis"], "max-of-n")
        self.assertNotIn("p95_basis", t4.event_stats([0.1] * 20))
        self.assertEqual(t4.event_stats([])["n"], 0)

    def _chain(self):
        return {"n_hooks": 3, "n_blocking": 2, "sum_s": 0.3, "max_s": 0.2, "sum_s_incl_async": 0.9,
                "sum_s_lower_bound_hooks": 0.2,
                "hooks": [
                    {"hook": "hooks/a.py", "event": "UserPromptSubmit", "blocking": True,
                     "runs_ms": [100.0, 120.0], "median_ms": 110.0, "first_ms": 100.0,
                     "returncode": 0, "denied": [], "path_gaps": []},
                    {"hook": "hooks/b.py", "event": "UserPromptSubmit", "blocking": True,
                     "runs_ms": [200.0, 180.0], "median_ms": 190.0, "first_ms": 200.0,
                     "returncode": 0, "denied": [], "path_gaps": ["herdr"]},
                    {"hook": "hooks/c.py", "event": "UserPromptSubmit", "blocking": False,
                     "runs_ms": [600.0, 600.0], "median_ms": 600.0, "first_ms": 600.0,
                     "returncode": 0, "denied": [], "path_gaps": []}]}

    def test_chain_events_use_blocking_hooks_per_repeat(self):
        chain = self._chain()
        self.assertEqual(t4.chain_rep_samples(chain), [0.3, 0.3])
        events, segments = t4.chain_events({"chains": {"claude:prompt": chain}})
        event = events["claude:prompt"]
        self.assertEqual((event["n"], event["median_s"], event["sequential_upper_bound_s"],
                          event["parallel_lower_bound_s"], event["n_blocking"]), (2, 0.3, 0.3, 0.2, 2))
        self.assertEqual(event["sum_s_lower_bound_hooks"], 0.2)
        self.assertEqual([item["blocking"] for item in segments["claude:prompt"]], [True, True, False])

    def test_opencode_events(self):
        def run(ms, sync, detached):
            return {"ms": ms, "sync_children": sync, "detached_children": detached, "commands": []}
        payload = {"import_ms": 12.0, "events": {
            "chat.message": {"runs": [run(100, 2, 1), run(300, 2, 1)], "median_ms": 200,
                             "sync_children_median": 2, "detached_children_median": 1, "threw": []},
            "tool.execute.before": {"runs": [run(10, 1, 0), run(30, 1, 0)], "median_ms": 20,
                                    "sync_children_median": 1, "detached_children_median": 0, "threw": []},
            "tool.execute.after": {"runs": [run(40, 3, 1), run(60, 3, 1)], "median_ms": 50,
                                   "sync_children_median": 3, "detached_children_median": 1, "threw": []}}}
        events, segments = t4.opencode_events(payload)
        self.assertEqual(events["opencode:prompt"]["samples_s"], [0.1, 0.3])
        self.assertEqual(events["opencode:tool"]["samples_s"], [0.05, 0.09])
        self.assertEqual(events["opencode:tool"]["sync_children_median"], 4)
        self.assertEqual(events["opencode:tool"]["detached_children_median"], 1)
        self.assertEqual(segments["plugin_import_ms"], 12.0)

    def test_measure_discards_numbers_on_violation(self):
        rig = Rig(self)
        _settings(rig.code, "claude", {"UserPromptSubmit": [(None, f"echo x > '{rig.code}/n.txt'", {})]})
        rig.commit()
        result = t4.measure({"base": str(rig.state), "marker": rig.marker}, reps=1,
                            code_root=rig.code, ops_roots=[rig.ops], settle_s=0,
                            specs=("claude:prompt",), include_opencode=False)
        self.assertEqual(result["metric"], "T4")
        self.assertEqual(result["events"], {})
        self.assertTrue(result["violations"])
        self.assertEqual(result["contract"], "python-deny+fingerprint")
        self.assertTrue(result["not_measured"])

    def test_measure_shape_without_violation(self):
        rig = Rig(self)
        _settings(rig.code, "claude", {"UserPromptSubmit": [(None, "true", {}), (None, "true", {"async": True})]})
        rig.commit()
        result = t4.measure({"base": str(rig.state), "marker": rig.marker}, reps=2,
                            code_root=rig.code, ops_roots=[rig.ops], settle_s=0,
                            specs=("claude:prompt",), include_opencode=False)
        self.assertEqual(result["violations"], [])
        event = result["events"]["claude:prompt"]
        self.assertEqual((event["n"], event["n_hooks"], event["n_blocking"]), (2, 2, 1))
        self.assertEqual(len(result["segments"]["claude:prompt"]), 2)
        self.assertTrue(any("execution order" in item["what"] for item in result["not_measured"]))


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class OpenCodeChainTests(unittest.TestCase):
    def test_counts_sync_and_detached_children_of_a_fake_plugin(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            plugin = tmp / "code" / "adapters" / "opencode" / "plugins"
            plugin.mkdir(parents=True)
            (plugin / "hearting-guards.js").write_text("\n".join([
                'import { spawnSync, spawn } from "node:child_process"',
                "const node = process.execPath",
                "export const AgentHarnessGuards = async (ctx) => ({",
                '  "chat.message": async () => {',
                '    spawnSync(node, ["-e", ""]); spawnSync(node, ["-e", ""])',
                '    spawn(node, ["-e", ""], { stdio: "ignore" })',
                "  },",
                '  "tool.execute.before": async () => { spawnSync(node, ["-e", ""]) },',
                '  "tool.execute.after": async () => { spawn(node, ["-e", ""], { stdio: "ignore" }); throw new Error("boom") },',
                "})",
            ]), encoding="utf-8")
            completed = subprocess.run(
                ["node", str(t4.OPENCODE_SCRIPT), "--code-root", str(tmp / "code"), "--directory", str(tmp),
                 "--reps", "2", "--session", "s"],
                cwd=str(tmp), capture_output=True, text=True, timeout=120)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            payload = json.loads(completed.stdout.strip().splitlines()[-1])
            events = payload["events"]
            self.assertEqual(payload["reps"], 2)
            self.assertEqual(events["chat.message"]["sync_children_median"], 2)
            self.assertEqual(events["chat.message"]["detached_children_median"], 1)
            self.assertEqual(events["tool.execute.before"]["sync_children_median"], 1)
            self.assertEqual(events["tool.execute.before"]["detached_children_median"], 0)
            self.assertEqual(events["tool.execute.after"]["detached_children_median"], 1)
            self.assertEqual(events["tool.execute.after"]["threw"], ["boom"])
            self.assertEqual(len(events["chat.message"]["runs"]), 2)
            translated, _segments = t4.opencode_events(payload)
            self.assertEqual(translated["opencode:prompt"]["n"], 2)


if __name__ == "__main__":
    unittest.main()
