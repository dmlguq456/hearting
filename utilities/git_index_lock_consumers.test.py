#!/usr/bin/env python3
"""Consumer-path and native adapter fixture regressions for index-lock failures."""
from __future__ import annotations

import importlib.util
import json
import re
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node") or "node"
MODULE_PATH = ROOT / "utilities" / "git_index_lock.py"
SPEC = importlib.util.spec_from_file_location("git_index_lock", MODULE_PATH)
git_index_lock = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(git_index_lock)


class GitIndexLockConsumerTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="hearting-lock-consumer-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo with ' quote"
        self.repo.mkdir()
        self.git("init", "-q")
        self.git("config", "user.name", "Fixture")
        self.git("config", "user.email", "fixture@example.invalid")
        (self.repo / "tracked.txt").write_text("base\n", encoding="utf-8")
        self.git("add", "tracked.txt")
        self.git("commit", "-qm", "base")
        (self.repo / "tracked.txt").write_text("changed\n", encoding="utf-8")
        self.git("add", "tracked.txt")
        index = self.git("rev-parse", "--path-format=absolute", "--git-path", "index").stdout.decode().strip()
        self.lock = Path(index + ".lock")
        self.lock.write_bytes(b"adapter fixture lock")
        self.lock_fd = os.open(self.lock, os.O_RDONLY)
        self.addCleanup(os.close, self.lock_fd)
        self.lock.chmod(0)
        self.result = self.git("commit", "-m", "expected failure", check=False)
        self.command = "git -C " + shlex.quote(str(self.repo)) + " commit -m 'expected failure'"

    def git(self, *args, check=True):
        return subprocess.run(["git", "-C", str(self.repo), *args], cwd=self.root,
                              capture_output=True, check=check)

    @staticmethod
    def response_text(value):
        return value.decode("utf-8", errors="replace")

    def codex_payload(self):
        return {
            "session_id": "session-fixture",
            "turn_id": "turn-fixture",
            "tool_use_id": "call-fixture",
            "cwd": str(self.root),
            "hook_event_name": "PostToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": self.command},
            "tool_response": {
                "exit_code": self.result.returncode,
                "stdout": self.response_text(self.result.stdout),
                "stderr": self.response_text(self.result.stderr),
            },
        }

    def run_bridge(self, harness, payload):
        return subprocess.run(
            ["python3", str(ROOT / "hooks/git-index-lock-context.py"), "--harness", harness],
            cwd=ROOT, input=json.dumps(payload), text=True, capture_output=True, check=False,
        )

    def test_codex_original_bash_event_adds_context_only_to_bound_nonzero(self):
        payload = self.codex_payload()
        output = self.run_bridge("codex", payload)
        self.assertEqual(output.returncode, 0, output.stderr)
        event = json.loads(output.stdout)
        context = event["hookSpecificOutput"]["additionalContext"]
        self.assertEqual(event["hookSpecificOutput"]["hookEventName"], "PostToolUse")
        self.assertIn(str(self.lock), context)
        self.assertIn("Ownership: unknown", context)

        payload["tool_response"]["exit_code"] = 0
        self.assertEqual(self.run_bridge("codex", payload).stdout, "")
        payload["tool_response"]["exit_code"] = 128
        payload["tool_response"]["stderr"] = (
            "fatal: Unable to create '/tmp/unrelated/index.lock': File exists.\n")
        self.assertEqual(self.run_bridge("codex", payload).stdout, "")

    def test_codex_unified_exec_uses_its_native_cmd_and_workdir_fields(self):
        payload = self.codex_payload()
        payload["tool_name"] = "exec_command"
        payload["tool_input"] = {
            "cmd": "git commit -m 'expected failure'",
            "workdir": str(self.repo),
        }
        payload["cwd"] = str(self.root)
        output = self.run_bridge("codex", payload)
        self.assertEqual(output.returncode, 0, output.stderr)
        event = json.loads(output.stdout)
        self.assertIn(str(self.lock), event["hookSpecificOutput"]["additionalContext"])
        self.assertIn("Ownership: unknown", event["hookSpecificOutput"]["additionalContext"])

    def test_codex_native_text_response_without_terminal_status_is_not_reclassified(self):
        payload = self.codex_payload()
        payload["tool_response"] = self.response_text(self.result.stderr)
        output = self.run_bridge("codex", payload)
        self.assertEqual(output.returncode, 0, output.stderr)
        self.assertEqual(output.stdout, "")
        payload["tool_response"] = "command completed without an index-lock failure"
        self.assertEqual(self.run_bridge("codex", payload).stdout, "")

    def test_python_wrapped_git_add_uses_only_the_selected_worktree_default_index(self):
        code = "import subprocess; subprocess.run(['git', 'add', 'tracked.txt'], check=True)"
        wrapped = "python3 -c " + shlex.quote(code) + "\n"
        failed = subprocess.run(["python3", "-c", code], cwd=self.repo,
                                capture_output=True, text=True, timeout=10, check=False)
        self.assertNotEqual(failed.returncode, 0)
        output = failed.stdout + failed.stderr
        env = {"AGENT_DISPATCH_WORKTREE": str(self.repo)}
        with mock.patch.dict(os.environ, env, clear=False):
            self.assertTrue(git_index_lock.codex_command_candidate(
                wrapped, str(self.root), failed.returncode, output, env=env))
            codex = git_index_lock.diagnose_codex_command(
                wrapped, str(self.root), failed.returncode, output, env=env)
            codex_hook = git_index_lock.diagnose_hook_payload({
                "hook_event_name": "PostToolUse", "tool_name": "exec_command",
                "cwd": str(self.root), "tool_input": {"cmd": wrapped, "workdir": str(self.root)},
                "tool_response": {"exit_code": failed.returncode,
                                  "stdout": failed.stdout, "stderr": failed.stderr},
            }, "codex")
            claude_hook = git_index_lock.diagnose_hook_payload({
                "hook_event_name": "PostToolUseFailure", "tool_name": "Bash",
                "cwd": str(self.root), "tool_input": {"command": wrapped},
                "error": f"Exit code {failed.returncode}\n{failed.stderr}",
            }, "claude")
            opencode = git_index_lock.diagnose_opencode_command(
                wrapped, str(self.root), failed.returncode, output)
            unbound = git_index_lock.diagnose_codex_command(
                wrapped, str(self.root), failed.returncode,
                output.replace(str(self.lock), "/tmp/unrelated/index.lock"), env=env)
        for diagnosis in (codex, codex_hook, claude_hook, opencode):
            with self.subTest(state=diagnosis["state"] if diagnosis else None):
                self.assertIsNotNone(diagnosis)
                self.assertEqual(diagnosis["state"], "present")
                self.assertEqual(diagnosis["lock_path"], str(self.lock))
                self.assertEqual(diagnosis["cleanup"]["outcome"], "not-attempted")
                self.assertIn("matching text is advisory", diagnosis["message"])
                self.assertNotIn("may retry", diagnosis["message"])
        self.assertEqual(unbound["state"], "unbound")
        self.assertTrue(self.lock.exists())
        self.assertFalse(git_index_lock.codex_command_candidate(
            wrapped, str(self.root), failed.returncode, output,
            env={**env, "GIT_INDEX_FILE": "alternate-index"}))
        self.assertTrue(git_index_lock.codex_command_candidate(
            wrapped, str(self.root), failed.returncode, output,
            env={**env, "GIT_CONFIG_GLOBAL": os.devnull}))
        self.assertFalse(git_index_lock.codex_command_candidate(
            wrapped, str(self.root), failed.returncode, output,
            env={**env, "GIT_CONFIG_COUNT": "1"}))

    def test_forged_python_eexist_output_is_advisory_and_preserves_selected_lock(self):
        before = os.fstat(self.lock_fd)
        identity = (before.st_dev, before.st_ino, before.st_mode, before.st_size,
                    before.st_mtime_ns, before.st_ctime_ns)
        original_bytes = os.pread(self.lock_fd, 4096, 0)
        forged = f"fatal: Unable to create '{self.lock}': File exists.\n"
        code = f"import sys; sys.stderr.write({forged!r}); sys.exit(128)"
        command = "python3 -c " + shlex.quote(code)
        child = subprocess.run(["python3", "-c", code], cwd=self.repo,
                               capture_output=True, text=True, timeout=10, check=False)
        self.assertEqual(child.returncode, 128)
        self.assertEqual(child.stderr, forged)

        env = {"AGENT_DISPATCH_WORKTREE": str(self.repo)}
        with mock.patch.dict(os.environ, env, clear=False):
            self.assertTrue(git_index_lock.codex_command_candidate(
                command, str(self.root), child.returncode, child.stderr, env=env))
            diagnosis = git_index_lock.diagnose_codex_command(
                command, str(self.root), child.returncode, child.stderr, env=env)
        self.assertIsNotNone(diagnosis)
        self.assertEqual(diagnosis["state"], "present")
        self.assertEqual(diagnosis["lock_path"], str(self.lock))
        self.assertEqual(diagnosis["cleanup"]["outcome"], "not-attempted")
        self.assertIn("matching text is advisory and does not prove Git returned EEXIST",
                      diagnosis["message"])
        after = os.fstat(self.lock_fd)
        self.assertEqual((after.st_dev, after.st_ino, after.st_mode, after.st_size,
                          after.st_mtime_ns, after.st_ctime_ns), identity)
        self.assertEqual(os.pread(self.lock_fd, 4096, 0), original_bytes)
        self.assertTrue(self.lock.exists())

    def test_codex_app_server_command_result_uses_aggregated_output_without_forging_streams(self):
        command = "zsh -lc " + shlex.quote(self.command)
        output = self.response_text(self.result.stderr)
        diagnosis = git_index_lock.diagnose_codex_command(
            command, str(self.root), self.result.returncode, output)
        self.assertEqual(diagnosis["state"], "present")
        self.assertEqual(diagnosis["lock_path"], str(self.lock))
        self.assertIn("Codex App Server aggregatedOutput", diagnosis["message"])
        self.assertIn("Stdout/stderr separation or raw bytes may be unavailable",
                      diagnosis["message"])

        self.assertIsNone(git_index_lock.diagnose_codex_command(
            command, str(self.root), 0, output))
        self.assertIsNone(git_index_lock.diagnose_codex_command(
            command, str(self.root), 130, output))
        with mock.patch.dict(os.environ, {"AGENT_DISPATCH_WORKTREE": str(self.repo)},
                             clear=False):
            fallback = git_index_lock.diagnose_codex_command(
                "zsh -lc 'git commit && echo unsafe'", str(self.root),
                self.result.returncode, output)
        self.assertEqual(fallback["state"], "present")
        self.assertEqual(fallback["lock_path"], str(self.lock))
        self.assertIn("replays the failed command", fallback["message"])
        unrelated = output.replace(str(self.lock), "/tmp/unrelated/index.lock")
        unbound = git_index_lock.diagnose_codex_command(
            command, str(self.root), self.result.returncode, unrelated)
        self.assertEqual(unbound["state"], "unbound")

    def test_codex_app_server_unknown_git_path_stays_unbound(self):
        command = "zsh -lc " + shlex.quote(self.command)
        output = self.response_text(self.result.stderr)
        with mock.patch.dict(os.environ, {"PATH": str(self.root / "empty-bin")}):
            diagnosis = git_index_lock.diagnose_codex_command(
                command, str(self.root), self.result.returncode, output)
        self.assertEqual(diagnosis["state"], "unbound")
        self.assertTrue(self.lock.exists())

    def test_real_rebase_start_and_continue_failures_reach_opencode_callback_once(self):
        real_git = shutil.which("git")
        self.assertIsNotNone(real_git)
        fake_bin = self.root / "counted-git-bin"
        fake_bin.mkdir()
        counter = self.root / "git-operation-counts"
        wrapper = fake_bin / "git"
        wrapper.write_text(
            "#!/usr/bin/python3\n"
            "import os, sys\n"
            "args = sys.argv[1:]\n"
            "label = 'continue' if '--continue' in args else 'start' if 'rebase' in args else None\n"
            f"if label: open({str(counter)!r}, 'a', encoding='utf-8').write(label + '\\n')\n"
            f"os.execv({real_git!r}, ['git', *args])\n",
            encoding="utf-8",
        )
        wrapper.chmod(0o755)
        env = {**os.environ, "PATH": f"{fake_bin}:{os.environ['PATH']}",
               "HEARTING_GIT_CALL_COUNT_FILE": str(counter), "AGENT_HOME": str(ROOT),
               "AGENT_SESSION_ROLE": "worker"}

        def git(repo, *args, check=True):
            return subprocess.run([real_git, "-C", str(repo), *args], cwd=repo,
                                  capture_output=True, check=check)

        def conflicted_repo(name):
            repo = self.root / name
            repo.mkdir()
            subprocess.run([real_git, "init", "-q", str(repo)], check=True)
            git(repo, "config", "user.name", "Fixture")
            git(repo, "config", "user.email", "fixture@example.invalid")
            tracked = repo / "tracked.txt"
            tracked.write_text("base\n", encoding="utf-8")
            git(repo, "add", "tracked.txt")
            git(repo, "commit", "-qm", "base")
            git(repo, "branch", "-m", "main")
            git(repo, "checkout", "-qb", "feature")
            tracked.write_text("feature\n", encoding="utf-8")
            git(repo, "add", "tracked.txt")
            git(repo, "commit", "-qm", "feature")
            git(repo, "checkout", "-q", "main")
            tracked.write_text("main\n", encoding="utf-8")
            git(repo, "add", "tracked.txt")
            git(repo, "commit", "-qm", "main")
            git(repo, "checkout", "-q", "feature")
            return repo, tracked

        def snapshot(repo, tracked, lock_fd, lock):
            index = git(repo, "rev-parse", "--path-format=absolute", "--git-path", "index").stdout.decode().strip()
            gitdir = Path(git(repo, "rev-parse", "--path-format=absolute", "--git-dir").stdout.decode().strip())
            rebase_state = []
            for dirname in ("rebase-merge", "rebase-apply"):
                directory = gitdir / dirname
                if directory.is_dir():
                    rebase_state.append((dirname, sorted(
                        (str(path.relative_to(directory)), path.read_bytes())
                        for path in directory.rglob("*") if path.is_file())))
            st = os.fstat(lock_fd)
            return (git(repo, "rev-parse", "HEAD").stdout, Path(index).read_bytes(),
                    tracked.read_bytes(), rebase_state,
                    (os.lstat(lock).st_dev, os.lstat(lock).st_ino, os.lstat(lock).st_mode,
                     st.st_size, os.pread(lock_fd, 256, 0)))

        def invoke_adapter(repo, command, result):
            combined = self.response_text(result.stdout) + self.response_text(result.stderr)
            script = f'''import {{ AgentHarnessGuards }} from {json.dumps((ROOT / "adapters/opencode/plugins/hearting-guards.js").as_uri())};
const hooks = await AgentHarnessGuards({{ directory: {json.dumps(str(repo))},
  worktree: {json.dumps(str(repo))}, client: {{}} }});
const input = {{ tool: "bash", sessionID: "ses_rebase_fixture", args: {{
  command: {json.dumps(command)}, workdir: {json.dumps(str(repo))}
}} }};
const output = {{ args: input.args, output: {json.dumps(combined)},
  metadata: {{ exit: {result.returncode}, output: {json.dumps(combined[:160])}, truncated: false }} }};
await hooks["tool.execute.after"](input, output);
console.log(JSON.stringify({{ output: output.output, metadata: output.metadata }}));
'''
            return subprocess.run([NODE, "--input-type=module", "-e", script], cwd=ROOT,
                                  env=env, text=True, capture_output=True, timeout=15)

        scenarios = []
        start_repo, start_file = conflicted_repo("rebase-start-repo")
        start_lock = Path(git(start_repo, "rev-parse", "--path-format=absolute",
                               "--git-path", "index").stdout.decode().strip() + ".lock")
        start_lock.write_bytes(b"rebase-start lock")
        start_fd = os.open(start_lock, os.O_RDONLY)
        self.addCleanup(os.close, start_fd)
        start_lock.chmod(0)
        start_command = ["git", "rebase", "main"]
        start_result = subprocess.run(["git", *start_command[1:]], cwd=start_repo, env=env,
                                      capture_output=True, check=False)
        self.assertNotEqual(start_result.returncode, 0)
        scenarios.append(("start", start_repo, start_file, start_lock, start_fd,
                          start_command, start_result))

        continue_repo, continue_file = conflicted_repo("rebase-continue-repo")
        conflict = git(continue_repo, "rebase", "main", check=False)
        self.assertNotEqual(conflict.returncode, 0)
        self.assertTrue((continue_repo / ".git/rebase-merge").is_dir())
        continue_file.write_text("resolved\n", encoding="utf-8")
        git(continue_repo, "add", "tracked.txt")
        continue_lock = Path(git(continue_repo, "rev-parse", "--path-format=absolute",
                                 "--git-path", "index").stdout.decode().strip() + ".lock")
        continue_lock.write_bytes(b"rebase-continue lock")
        continue_fd = os.open(continue_lock, os.O_RDONLY)
        self.addCleanup(os.close, continue_fd)
        continue_lock.chmod(0)
        continue_command = ["git", "-c", "core.editor=true", "rebase", "--continue"]
        continue_result = subprocess.run(["git", *continue_command[1:]], cwd=continue_repo,
                                         env=env, capture_output=True, check=False)
        self.assertNotEqual(continue_result.returncode, 0)
        scenarios.append(("continue", continue_repo, continue_file, continue_lock,
                          continue_fd, continue_command, continue_result))

        for label, repo, tracked, lock, lock_fd, command, result in scenarios:
            with self.subTest(rebase=label):
                before = snapshot(repo, tracked, lock_fd, lock)
                callback = invoke_adapter(repo, shlex.join(command), result)
                self.assertEqual(callback.returncode, 0, callback.stderr + callback.stdout)
                delivered = json.loads(callback.stdout.strip().splitlines()[-1])
                self.assertEqual(delivered["metadata"], {
                    "exit": result.returncode,
                    "output": (self.response_text(result.stdout) + self.response_text(result.stderr))[:160],
                    "truncated": False,
                })
                self.assertTrue(delivered["output"].startswith(
                    self.response_text(result.stdout) + self.response_text(result.stderr)))
                self.assertIn(self.response_text(result.stderr), delivered["output"])
                self.assertIn(str(lock), delivered["output"])
                self.assertIn("OpenCode combined output", delivered["output"])
                self.assertIn("Ownership: unknown", delivered["output"])
                self.assertEqual(snapshot(repo, tracked, lock_fd, lock), before)
                self.assertEqual(counter.read_text(encoding="utf-8").splitlines().count(label), 1)

    def test_claude_post_tool_use_failure_uses_native_error_fields(self):
        payload = {
            "session_id": "claude-session-fixture",
            "cwd": str(self.root),
            "hook_event_name": "PostToolUseFailure",
            "tool_name": "Bash",
            "tool_use_id": "claude-call-fixture",
            "tool_input": {"command": self.command},
            "error": f"Exit code {self.result.returncode}\n{self.response_text(self.result.stderr)}",
        }
        output = self.run_bridge("claude", payload)
        self.assertEqual(output.returncode, 0, output.stderr)
        event = json.loads(output.stdout)
        self.assertEqual(event["hookSpecificOutput"]["hookEventName"], "PostToolUseFailure")
        self.assertIn(str(self.lock), event["hookSpecificOutput"]["additionalContext"])
        self.assertIn("Ownership: unknown", event["hookSpecificOutput"]["additionalContext"])
        payload["tool_name"] = "Read"
        self.assertEqual(self.run_bridge("claude", payload).stdout, "")

    def test_native_configurations_wire_only_the_original_shell_result_events(self):
        codex = json.loads((ROOT / "adapters/codex/hooks/hooks.json").read_text(encoding="utf-8"))
        codex_hooks = codex["hooks"]
        self.assertFalse(any(
            "git-index-lock-context.py" in json.dumps(group)
            for event_groups in codex_hooks.values() for group in event_groups))
        self.assertTrue(any(
            group.get("matcher") == "*"
            and any("posttooluse-interaction-clear.py" in hook.get("command", "")
                    for hook in group.get("hooks", []))
            for group in codex_hooks["PostToolUse"]))
        claude = json.loads((ROOT / "adapters/claude/settings.json").read_text(encoding="utf-8"))
        for event_name in ("PostToolUse", "PostToolUseFailure"):
            self.assertTrue(any(
                group.get("matcher") == "Bash"
                and any("git-index-lock-context.py" in hook.get("command", "")
                        for hook in group.get("hooks", []))
                for group in claude["hooks"][event_name]))

    def invoke_opencode_callback(self, *, instance_directory, workdir, metadata, combined_output):
        context = {"client": {}}
        if instance_directory is not None:
            context["directory"] = str(instance_directory)
            context["worktree"] = str(instance_directory)
        args = {"command": "git commit -m 'expected failure'"}
        if workdir is not None:
            args["workdir"] = workdir
        script = f'''import {{ AgentHarnessGuards }} from {json.dumps((ROOT / "adapters/opencode/plugins/hearting-guards.js").as_uri())};
const hooks = await AgentHarnessGuards({json.dumps(context)});
const input = {{ tool: "bash", args: {json.dumps(args)} }};
const output = {{ args: input.args, output: {json.dumps(combined_output)},
  metadata: {json.dumps(metadata)} }};
const originalMetadata = structuredClone(output.metadata);
const originalOutput = output.output;
await hooks["tool.execute.after"](input, output);
console.log(JSON.stringify({{ output: output.output, metadata: output.metadata,
  originalMetadata, originalOutput }}));
'''
        env = {
            **os.environ,
            "HOME": str(self.root / "home"),
            "XDG_CONFIG_HOME": str(self.root / "config"),
            "XDG_DATA_HOME": str(self.root / "data"),
            "XDG_STATE_HOME": str(self.root / "state"),
            "XDG_CACHE_HOME": str(self.root / "cache"),
            "FLEET_SESSION_REGISTRY_DIR": str(self.root / "state/sessions"),
            "AGENT_HOME": str(ROOT),
            "AGENT_SESSION_ROLE": "worker",
            "OPENCODE_VERSION": "1.19.0",
        }
        return subprocess.run([NODE, "--input-type=module", "-e", script], cwd=ROOT,
                              env=env, text=True, capture_output=True, timeout=15)

    def test_actual_opencode_callback_uses_native_result_and_effective_workdir(self):
        combined = self.response_text(self.result.stdout) + self.response_text(self.result.stderr)
        native_metadata = {
            "exit": self.result.returncode,
            "output": combined[:160],
            "truncated": False,
        }
        scenarios = (
            ("omitted", self.repo, None),
            ("relative", self.root, str(self.repo.relative_to(self.root))),
            ("explicit", self.root, str(self.repo)),
        )
        for label, instance_directory, workdir in scenarios:
            with self.subTest(workdir=label):
                run = self.invoke_opencode_callback(
                    instance_directory=instance_directory, workdir=workdir,
                    metadata=native_metadata, combined_output=combined,
                )
                self.assertEqual(run.returncode, 0, run.stderr + run.stdout)
                result = json.loads(run.stdout.strip().splitlines()[-1])
                self.assertEqual(result["metadata"], native_metadata)
                self.assertEqual(result["originalMetadata"], native_metadata)
                self.assertEqual(result["originalOutput"], combined)
                self.assertTrue(result["output"].startswith(combined))
                self.assertIn(str(self.lock), result["output"])
                self.assertIn("OpenCode combined output", result["output"])
                self.assertIn("Ownership: unknown", result["output"])

        untouched = (
            ("success-conflicting-fields", self.root, str(self.repo),
             {**native_metadata, "exit": 0, "exitCode": 128, "stdout": combined, "stderr": combined}, combined),
            ("null-conflicting-fields", self.root, str(self.repo),
             {**native_metadata, "exit": None, "exitCode": 128, "stdout": combined, "stderr": combined}, combined),
            ("signal", self.root, str(self.repo), {**native_metadata, "exit": 130}, combined),
            ("malformed-exit", self.root, str(self.repo), {**native_metadata, "exit": "128"}, combined),
            ("unrelated", self.root, str(self.repo), native_metadata,
             "fatal: unrelated command failure\n"),
            ("context-unavailable", None, str(self.repo), native_metadata, combined),
        )
        for label, instance_directory, workdir, metadata, output in untouched:
            with self.subTest(untouched=label):
                run = self.invoke_opencode_callback(
                    instance_directory=instance_directory, workdir=workdir,
                    metadata=metadata, combined_output=output,
                )
                self.assertEqual(run.returncode, 0, run.stderr + run.stdout)
                result = json.loads(run.stdout.strip().splitlines()[-1])
                self.assertEqual(result["metadata"], metadata)
                self.assertEqual(result["originalMetadata"], metadata)
                self.assertEqual(result["output"], output)
                self.assertEqual(result["originalOutput"], output)



class ReadOnlyStatusCallersTest(unittest.TestCase):
    """A timed read-only `git status` must not take the optional index lock.

    Killed by its timeout while refreshing the index, such a status leaves a
    holderless 0-byte index.lock that refuses every later add/commit in that
    worktree (2026-10-17: the Claude statusline's 2 s status on a dispatch
    worktree, and route-presence-gate's 20 s one)."""

    STATUS = re.compile(r'"status",\s*"--porcelain')
    MARKERS = ("GIT_OPTIONAL_LOCKS", "git_optional_locks_env", "_readonly_git_env", "--no-optional-locks")

    def sources(self):
        yield ROOT / "adapters" / "claude" / "statusline.sh"
        for directory in (ROOT / "utilities", ROOT / "hooks"):
            for path in sorted(directory.glob("*.py")):
                if not path.name.endswith(".test.py"):
                    yield path

    def test_every_read_only_status_caller_skips_the_optional_index_lock(self):
        checked, missing = 0, []
        for path in self.sources():
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            wrapper_env = any(marker in "\n".join(lines) for marker in self.MARKERS)
            for number, line in enumerate(lines):
                if not self.STATUS.search(line):
                    continue
                checked += 1
                window = "\n".join(lines[number:number + 8])
                via_wrapper = "_git(" in line and wrapper_env
                if not via_wrapper and not any(marker in window for marker in self.MARKERS):
                    missing.append(f"{path.relative_to(ROOT)}:{number + 1}")
        self.assertGreaterEqual(checked, 6)
        self.assertEqual(missing, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
