#!/usr/bin/env python3
"""The route presence gate: one fact, three payload shapes, fail-open everywhere else."""
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
GATE = HERE / "route_presence_gate.py"
RESOLVER = HERE / "artifact-root.sh"
_spec = importlib.util.spec_from_file_location("route_presence_gate", GATE)
G = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(G)

SID = "11111111-2222-3333-4444-555555555555"


def git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True,
                   env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"})


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(os.path.realpath(tempfile.mkdtemp(prefix="route-gate-")))
        self.home = self.tmp / "home"
        self.home.mkdir()
        scratch = self.tmp / "scratch"
        scratch.mkdir()
        self.repo = self.tmp / "work" / "proj"
        self.repo.mkdir(parents=True)
        git(self.repo, "init", "-q")
        (self.repo / "src").mkdir()
        (self.repo / "src" / "engine.py").write_text("x = 1\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "init")
        self.root = self.repo / ".agent_reports"
        (self.root / "campaigns" / "2026-09-01_old-stream").mkdir(parents=True)
        (self.root / "campaigns" / "2026-10-01_new-stream").mkdir(parents=True)
        os.utime(self.root / "campaigns" / "2026-09-01_old-stream", (1, 1))
        self.ledgers = self.tmp / "ledgers"
        self.env = {
            "HOME": str(self.home), "PATH": os.environ.get("PATH", ""),
            "TMPDIR": str(scratch), "FLEET_ROUTE_CHAIN_DIR": str(self.ledgers),
            "XDG_DATA_HOME": str(self.home / ".local" / "share"),
        }
        self._tempdir = tempfile.tempdir
        tempfile.tempdir = str(scratch)
        # route_chain resolves its state root from the process environment.
        self._environ = mock.patch.dict(os.environ, {"FLEET_ROUTE_CHAIN_DIR": str(self.ledgers)})
        self._environ.start()

    def tearDown(self):
        self._environ.stop()
        tempfile.tempdir = self._tempdir
        subprocess.run(["rm", "-rf", str(self.tmp)], check=False)

    def ledger_line(self, root, harness="claude", sid=SID):
        directory = self.ledgers / harness
        directory.mkdir(parents=True, exist_ok=True)
        with open(directory / f"{sid}.jsonl", "a", encoding="utf-8") as handle:
            handle.write(json.dumps({"v": 1, "event": "compose", "harness": harness,
                                     "session_id": sid, "route_id": "rt-x",
                                     "artifact_root": str(root)}) + "\n")

    def edit(self, path, sid=SID, tool="Edit"):
        return {"tool_name": tool, "session_id": sid, "cwd": str(self.repo),
                "tool_input": {"file_path": str(path)}}

    def bash(self, command, sid=SID, cwd=None):
        return {"tool_name": "Bash", "session_id": sid, "cwd": str(cwd or self.repo),
                "tool_input": {"command": command}}

    def judge(self, payload, harness="claude", **extra):
        return G.judge(harness, payload, {**self.env, **extra})


class EditTriggerTest(Fixture):
    def test_read_only_status_disables_optional_index_refresh(self):
        result = subprocess.CompletedProcess([], 0, "", "")
        with mock.patch.object(G.subprocess, "run", return_value=result) as run:
            self.assertFalse(G._has_source_change(self.repo, str(self.root), self.env))
        self.assertEqual(run.call_args.kwargs["env"]["GIT_OPTIONAL_LOCKS"], "0")

    def test_first_source_edit_is_refused_with_the_compose_line(self):
        reason = self.judge(self.edit(self.repo / "src" / "engine.py"))
        self.assertIn(f"no route for {self.root}", reason)
        line = reason.splitlines()[1]
        self.assertTrue(line.startswith("python3 "), line)
        self.assertIn("capability-route.py compose --start --shape direct --prompt-file '<task>'", line)
        self.assertIn("--campaign-key new-stream", line)
        self.assertIn("--slug engine --cwd", line)  # the cycle folder carries the date
        self.assertIn(f"--cwd {self.repo}", line)
        self.assertIn("For one atomic, reversible inline change", reason)
        self.assertIn("make the edit in the worktree it prints", reason)
        self.assertIn("For other new work, omit --shape", reason)
        self.assertIn("the frame decides the route", reason)
        self.assertIn("person-specified route", reason)
        self.assertIn("capability-route.py start --route", reason)

    def test_a_managed_release_is_printed_as_its_current_pointer(self):
        hearting = self.home / ".local" / "share" / "hearting"
        release = hearting / "releases" / "v9.9.9"
        (release / "utilities").mkdir(parents=True)
        (release / "utilities" / "capability-route.py").write_text("")
        (release / "core").mkdir()
        (release / "core" / "CORE.md").write_text("")
        (hearting / "current").symlink_to(release)
        line = self.judge(self.edit(self.repo / "src" / "engine.py"),
                          AGENT_HOME=str(release)).splitlines()[1]
        self.assertIn(f"{hearting / 'current'}/utilities/capability-route.py compose", line)
        self.assertNotIn("releases/", line)

    def test_retry_passes_once_the_session_has_a_route_here(self):
        payload = self.edit(self.repo / "src" / "engine.py")
        self.assertTrue(self.judge(payload))
        self.ledger_line(self.root)
        self.assertEqual(self.judge(payload), "")
        self.assertEqual(self.judge(self.edit(self.repo / "src" / "new.py", tool="Write")), "")

    def test_any_earlier_route_line_counts_and_other_folders_do_not(self):
        self.ledger_line(self.tmp / "elsewhere" / ".agent_reports")
        self.assertTrue(self.judge(self.edit(self.repo / "src" / "engine.py")))
        self.ledger_line(self.root)  # e.g. a closed route from an hour ago
        self.assertEqual(self.judge(self.edit(self.repo / "src" / "engine.py")), "")

    def test_another_sessions_route_does_not_count(self):
        self.ledger_line(self.root, sid="99999999-0000-0000-0000-000000000000")
        self.assertTrue(self.judge(self.edit(self.repo / "src" / "engine.py")))

    def test_payload_and_environment_ids_are_both_read(self):
        env_sid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        self.ledger_line(self.root, sid=env_sid)
        payload = self.edit(self.repo / "src" / "engine.py")
        self.assertTrue(self.judge(payload))
        self.assertEqual(self.judge(payload, CLAUDE_CODE_SESSION_ID=env_sid), "")
        self.ledger_line(self.root, harness="codex", sid="thread-1")
        self.assertEqual(self.judge(payload, harness="codex", CODEX_THREAD_ID="thread-1"), "")

    def test_exempt_paths_pass(self):
        for path in (self.root / "plans" / "p.md",
                     self.repo / ".claude_reports" / "x.md",
                     self.repo / ".git" / "config",
                     Path(tempfile.gettempdir()) / "probe.py",
                     self.home / ".claude" / "settings.json",
                     self.tmp / "not-a-repo" / "a.py"):
            self.assertEqual(self.judge(self.edit(path)), "", path)

    def test_nested_dirs_named_like_artifact_roots_are_still_source(self):
        for name in (".agent_reports", ".claude_reports"):
            self.assertTrue(self.judge(self.edit(self.repo / "src" / name / "model.py")), name)
        (self.repo / "src" / ".agent_reports").mkdir()
        (self.repo / "src" / ".agent_reports" / "model.py").write_text("y = 1\n")
        self.assertTrue(self.judge(self.bash("git add -A && git commit -m model")))

    def test_an_overridden_canonical_root_passes(self):
        override = self.repo / "out-reports"  # inside the work tree, so only the root rule exempts it
        override.mkdir()
        payload = self.edit(override / "notes.md")
        self.assertEqual(self.judge(payload, AGENT_ARTIFACT_ROOT=str(override)), "")
        self.assertTrue(self.judge(self.edit(self.repo / "src" / "engine.py"),
                                   AGENT_ARTIFACT_ROOT=str(override)))

    def test_scratch_repo_in_the_temp_dir_passes(self):
        scratch_repo = Path(tempfile.gettempdir()) / "clone"
        scratch_repo.mkdir()
        git(scratch_repo, "init", "-q")
        self.assertEqual(self.judge(self.edit(scratch_repo / "a.py")), "")

    def test_codex_apply_patch_targets_are_judged(self):
        patch = "*** Begin Patch\n*** Update File: src/engine.py\n@@\n-x = 1\n+x = 2\n*** End Patch\n"
        payload = {"tool_name": "apply_patch", "session_id": SID, "cwd": str(self.repo),
                   "tool_input": {"command": ["apply_patch", patch]}}
        self.assertTrue(self.judge(payload, harness="codex"))
        artifact_patch = patch.replace("src/engine.py", ".agent_reports/notes.md")
        payload["tool_input"] = {"command": ["apply_patch", artifact_patch]}
        self.assertEqual(self.judge(payload, harness="codex"), "")


class ShellTriggerTest(Fixture):
    def test_commit_with_source_change_is_refused(self):
        (self.repo / "src" / "engine.py").write_text("x = 2\n")
        reason = self.judge(self.bash("git commit -am fix"))
        guidance = reason.splitlines()[1]
        self.assertIn("compose --shape direct", guidance)
        self.assertNotIn("--start", guidance)
        self.assertIn("records the route for changes already made in place", reason)
        for command in ("git commit -am fix", "git -C src commit -m 'a; b'",
                        f"cd {self.repo} && git add -A && git commit -m x",
                        "GIT_EDITOR=true git --no-pager commit", "echo hi\ngit commit -m x"):
            self.assertTrue(self.judge(self.bash(command, cwd=self.tmp if command.startswith("cd") else None)),
                            command)

    def test_commit_with_only_artifact_changes_passes(self):
        (self.root / "notes.md").write_text("n\n")
        self.assertEqual(self.judge(self.bash("git commit -am notes")), "")

    def test_long_runs_are_refused(self):
        for command in ("compute-hosts run gpu1 -- python train.py",
                        "python3 /opt/h/utilities/compute-hosts.py run gpu2 -- bash x.sh",
                        "nohup python3 src/train.py --cfg a > log 2>&1 &",
                        "setsid nohup python -m pkg.train_main &",
                        "nohup python run.py &"):
            self.assertTrue(self.judge(self.bash(command)), command)

    def test_ordinary_and_ambiguous_shell_passes(self):
        (self.repo / "src" / "engine.py").write_text("x = 2\n")
        for command in ("git status", "git log --oneline", "echo 'git commit -m x'",
                        "bash -c 'git commit -m x'", "cat <<EOF\ngit commit -m x\nEOF",
                        "python train.py", "nohup sleep 10 &", "compute-hosts status",
                        "git commit -m 'unterminated", "ls; pwd"):
            self.assertEqual(self.judge(self.bash(command)), "", command)

    def test_codex_exec_command_uses_its_workdir(self):
        (self.repo / "src" / "engine.py").write_text("x = 2\n")
        payload = {"tool_name": "functions.exec_command", "session_id": "thread-9",
                   "cwd": str(self.tmp), "tool_input": {"cmd": "git commit -am x", "workdir": str(self.repo)}}
        self.assertTrue(self.judge(payload, harness="codex"))
        payload["tool_input"]["workdir"] = str(self.tmp)
        self.assertEqual(self.judge(payload, harness="codex"), "")


class ExemptionTest(Fixture):
    def test_switch_workers_ci_pass(self):
        payload = self.edit(self.repo / "src" / "engine.py")
        for extra in ({"HEARTING_ROUTE_GATE": "off"}, {"HEARTING_ROUTE_GATE": "0"},
                      {"AGENT_DISPATCH_DEPTH": "2"}, {"AGENT_DISPATCH_DEPTH": "1"},
                      {"AGENT_SESSION_ROLE": "worker"}, {"AGENT_DISPATCH_CHILD": "1"},
                      {"OPENCODE_DISPATCH_SLUG": "w"}, {"CI": "true"}):
            self.assertEqual(self.judge(payload, **extra), "", extra)
        self.assertTrue(self.judge(payload, HEARTING_ROUTE_GATE="on", AGENT_DISPATCH_DEPTH="0", CI="false"))

    def test_dev_activation_passes(self):
        payload = self.edit(self.repo / "src" / "engine.py")
        self.assertEqual(self.judge(payload, AGENT_HOME=str(self.repo)), "")
        linked = self.tmp / "work" / "proj-wt"
        git(self.repo, "worktree", "add", "-q", "-b", "feat", str(linked))
        self.assertEqual(self.judge(self.edit(linked / "src" / "engine.py"), AGENT_HOME=str(self.repo)), "")

    def test_no_session_id_and_judgement_errors_fail_open(self):
        payload = self.edit(self.repo / "src" / "engine.py", sid="")
        self.assertEqual(self.judge(payload), "")
        good = self.edit(self.repo / "src" / "engine.py")
        with mock.patch.object(G, "_route_chain", side_effect=ImportError("gone")):
            self.assertEqual(self.judge(good), "")
        with mock.patch.object(G, "_ledger_roots", side_effect=PermissionError("denied")):
            self.assertEqual(self.judge(good), "")
        self.assertEqual(G.judge("claude", "not-a-dict", self.env), "")


class ArtifactRootParityTest(Fixture):
    def resolver(self, directory, **extra):
        env = {key: value for key, value in {**self.env, **extra}.items()}
        out = subprocess.run(["sh", str(RESOLVER), str(directory)], capture_output=True,
                             text=True, check=True, env=env).stdout.strip().splitlines()[-1]
        return os.path.realpath(out)

    def test_gate_root_equals_the_canonical_resolver(self):
        linked = self.tmp / "work" / "proj-wt"
        git(self.repo, "worktree", "add", "-q", "-b", "feat", str(linked))
        link = self.tmp / "via-link"
        link.symlink_to(self.repo)
        legacy = self.tmp / "work" / "legacy"
        legacy.mkdir()
        git(legacy, "init", "-q")
        (legacy / ".claude_reports").mkdir()
        bare = self.tmp / "work" / "fresh"
        bare.mkdir()
        git(bare, "init", "-q")
        for top in (self.repo, linked, link, legacy, bare):
            self.assertEqual(G.artifact_root(Path(top), self.env), self.resolver(top), top)
        override = self.tmp / "override"
        override.mkdir()
        self.assertEqual(G.artifact_root(self.repo, {**self.env, "AGENT_ARTIFACT_ROOT": str(override)}),
                         self.resolver(self.repo, AGENT_ARTIFACT_ROOT=str(override)))

    def test_linked_worktree_shares_the_primary_folder(self):
        linked = self.tmp / "work" / "proj-wt"
        git(self.repo, "worktree", "add", "-q", "-b", "feat", str(linked))
        self.ledger_line(self.root)
        self.assertEqual(self.judge(self.edit(linked / "src" / "engine.py")), "")


class ModeTest(Fixture):
    def run_mode(self, mode, payload):
        return subprocess.run([sys.executable, str(GATE), mode], input=json.dumps(payload),
                              capture_output=True, text=True, env=self.env)

    def test_claude_mode_exit_2_with_reason_on_stderr(self):
        result = self.run_mode("--claude", self.edit(self.repo / "src" / "engine.py"))
        self.assertEqual(result.returncode, 2)
        self.assertIn("capability-route.py compose --start --shape direct", result.stderr)
        self.assertEqual(result.stdout, "")
        self.ledger_line(self.root)
        self.assertEqual(self.run_mode("--claude", self.edit(self.repo / "src" / "engine.py")).returncode, 0)

    def test_codex_mode_blocks_with_json(self):
        result = self.run_mode("--codex", self.bash("compute-hosts run gpu1 -- python train.py", sid="thread-2"))
        self.assertEqual(result.returncode, 0)
        decision = json.loads(result.stdout)
        self.assertEqual(decision["decision"], "block")
        self.assertIn("--shape direct", decision["reason"])

    def test_opencode_mode_exit_1_with_reason_on_stdout(self):
        payload = {"tool": "write", "args": {"filePath": str(self.repo / "src" / "engine.py")},
                   "sessionID": "ses_abc", "cwd": str(self.repo)}
        result = self.run_mode("--opencode", payload)
        self.assertEqual(result.returncode, 1)
        self.assertIn("no route for", result.stdout)
        self.ledger_line(self.root, harness="opencode", sid="ses_abc")
        self.assertEqual(self.run_mode("--opencode", payload).returncode, 0)

    def test_unparsable_input_passes_in_every_mode(self):
        for mode in ("--claude", "--codex", "--opencode"):
            result = subprocess.run([sys.executable, str(GATE), mode], input="{not json",
                                    capture_output=True, text=True, env=self.env)
            self.assertEqual((result.returncode, result.stdout), (0, ""), mode)


if __name__ == "__main__":
    unittest.main()
