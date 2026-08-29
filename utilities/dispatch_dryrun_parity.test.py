#!/usr/bin/env python3
"""C-19 regression: dry-run and start must materialize the same command.

Before the fix, each wrapper cleared the public preview attempt id (the
`attempt_id=-` receipt) by nulling the *same* variable it later used to
gate state-root grants and prompt/log filenames, so a dry-run silently
dropped writable-root grants and used a different transcript name than the
matching start. The fix threads a preserved `command_attempt_id` through
materialization while keeping the public receipt scrubbed. These tests
build both variants' argv/Namespace by hand and compare -- no child process
is ever spawned.
"""
import argparse
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


CODEX = _load("codex_dryrun_parity_wh", ROOT / "adapters/codex/bin/dispatch-headless.py")
CLAUDE = _load("claude_dryrun_parity_wh", ROOT / "adapters/claude/bin/dispatch-headless.py")
OPENCODE = _load("opencode_dryrun_parity_wh", ROOT / "adapters/opencode/bin/dispatch-headless.py")

ATTEMPT = "att-parity-fixture01"


def _normalize(command: str, attempt_id: str) -> str:
    out = command.replace(attempt_id, "<ATTEMPT>")
    out = out.replace("unassigned", "<ATTEMPT>")
    out = re.sub(r"preview-only\.(json|lease)", r"<ATTEMPT>.\1", out)
    return out


def _codex_args(**overrides):
    base = dict(
        worker_type="stage", intensity="strong", worktree="/tmp/fixture-worktree",
        route_id=None, route_node="execute", attempt_id=None, route_file=None,
        worker_role=None, profile=None, capability="autopilot-code",
        capability_mode="dev", worker_mode=None, mode=None,
        qa="standard", dispatch_depth=2, parent_slug=None, parent_session_id=None,
        capability_owner=None, owner_harness=None, write_scope=None,
        completion_gate=None, assigned_contract=None, unit=None, model_role=None,
        agent_home=Path("/tmp/fixture-agent-home"), artifact_root="/tmp/fixture-artifacts",
        jobs_path=Path("/tmp/fixture-agent-home/.dispatch/jobs.log"),
        sandbox="workspace-write", approval="never",
        nested_headless_network=False,
        resolved_model_settings={
            "source": "inherit", "role": "-", "model": None, "reasoning": None
        },
        resolved_completion_delivery="one-shot",
        report_bundle_root=None, owner_route_binding=None, max_continuations=None,
        command_attempt_id=None,
    )
    base.update(overrides)
    return argparse.Namespace(**base)


def _claude_args(**overrides):
    base = dict(
        worker_type="stage", intensity="strong", artifact_root="/tmp/fixture-artifacts",
        worktree="/tmp/fixture-worktree", route_id=None,
        agent_home=Path("/tmp/fixture-agent-home"),
        jobs_path=Path("/tmp/jobs.log"),
        completion_gate=None, assigned_contract=None, unit=None,
        capability="autopilot-code", capability_mode="dev", worker_mode=None, mode=None,
        resolved_model_settings={"source": "inherit", "role": "-", "model": None, "effort": None},
        resolved_completion_delivery="one-shot",
        report_bundle_root=None, owner_route_binding=None, max_continuations=None,
        attempt_id=None, command_attempt_id=None,
    )
    base.update(overrides)
    return argparse.Namespace(**base)


def _opencode_args(**overrides):
    base = dict(
        worktree="/tmp/fixture-worktree", agent="build",
        resolved_model_settings={
            "source": "inherit", "model": None, "variant": "runtime-default"
        },
        attempt_id=None, command_attempt_id=None,
    )
    base.update(overrides)
    return argparse.Namespace(**base)


class CodexDryRunStartParity(unittest.TestCase):
    def test_exec_branch_command_is_identical_and_carries_state_root(self):
        dry = _codex_args(attempt_id=None, command_attempt_id=ATTEMPT, route_id="rt-parity-fixture")
        start = _codex_args(attempt_id=ATTEMPT, command_attempt_id=ATTEMPT, route_id="rt-parity-fixture")
        prompt_path, log_path = Path("/tmp/p.txt"), Path("/tmp/l.log")
        dry_cmd = CODEX.shell_command(dry, prompt_path, log_path)
        start_cmd = CODEX.shell_command(start, prompt_path, log_path)
        self.assertEqual(dry_cmd, start_cmd)
        state_root = str(CODEX.dispatch_state_root(dry.jobs_path))
        self.assertIn(f"--add-dir {state_root}", dry_cmd)
        self.assertIn(f"--add-dir {state_root}", start_cmd)

    def test_exec_branch_without_command_attempt_id_omits_state_root(self):
        # Sanity control: the state-root grant genuinely depends on a preserved
        # command_attempt_id, not on some other accidental condition.
        args = _codex_args(attempt_id=None, command_attempt_id=None)
        command = CODEX.shell_command(args, Path("/tmp/p.txt"), Path("/tmp/l.log"))
        state_root = str(CODEX.dispatch_state_root(args.jobs_path))
        self.assertNotIn(state_root, command)

    def test_app_server_branch_both_sides_carry_isolated_state_root(self):
        dry = _codex_args(
            attempt_id=None, command_attempt_id=ATTEMPT, route_id="rt-parity-fixture",
            resolved_completion_delivery="app-server-supervised",
        )
        start = _codex_args(
            attempt_id=ATTEMPT, command_attempt_id=ATTEMPT, route_id="rt-parity-fixture",
            resolved_completion_delivery="app-server-supervised",
        )
        prompt_path, log_path = Path("/tmp/p.txt"), Path("/tmp/l.log")
        dry_cmd = CODEX.shell_command(dry, prompt_path, log_path)
        start_cmd = CODEX.shell_command(start, prompt_path, log_path)
        state_root = str(CODEX.dispatch_state_root(dry.jobs_path))
        self.assertIn(f"--writable-root {state_root}", dry_cmd)
        self.assertIn(f"--writable-root {state_root}", start_cmd)
        self.assertEqual(_normalize(dry_cmd, ATTEMPT), _normalize(start_cmd, ATTEMPT))

    def test_writer_attempt_token_matches_command_attempt_id_on_both_sides(self):
        dry = _codex_args(attempt_id=None, command_attempt_id=ATTEMPT)
        start = _codex_args(attempt_id=ATTEMPT, command_attempt_id=ATTEMPT)
        prompt_path, log_path = Path("/tmp/p.txt"), Path("/tmp/l.log")
        dry_cmd = CODEX.shell_command(dry, prompt_path, log_path)
        start_cmd = CODEX.shell_command(start, prompt_path, log_path)
        self.assertIn(f"--attempt {ATTEMPT}", dry_cmd)
        self.assertIn(f"--attempt {ATTEMPT}", start_cmd)


class ClaudeDryRunStartParity(unittest.TestCase):
    def test_plain_print_branch_is_attempt_independent(self):
        dry = _claude_args(attempt_id=None, command_attempt_id=ATTEMPT)
        start = _claude_args(attempt_id=ATTEMPT, command_attempt_id=ATTEMPT)
        prompt_path, log_path = Path("/tmp/p.txt"), Path("/tmp/l.log")
        dry_cmd = CLAUDE.shell_command(dry, prompt_path, log_path)
        start_cmd = CLAUDE.shell_command(start, prompt_path, log_path)
        self.assertEqual(dry_cmd, start_cmd)
        self.assertNotIn(ATTEMPT, dry_cmd)

    def test_session_resume_branch_normalizes_to_identical(self):
        dry = _claude_args(
            attempt_id=None, command_attempt_id=ATTEMPT,
            resolved_completion_delivery="session-resume-supervised",
        )
        start = _claude_args(
            attempt_id=ATTEMPT, command_attempt_id=ATTEMPT,
            resolved_completion_delivery="session-resume-supervised",
        )
        prompt_path, log_path = Path("/tmp/p.txt"), Path("/tmp/l.log")
        dry_cmd = CLAUDE.shell_command(dry, prompt_path, log_path)
        start_cmd = CLAUDE.shell_command(start, prompt_path, log_path)
        self.assertEqual(_normalize(dry_cmd, ATTEMPT), _normalize(start_cmd, ATTEMPT))
        self.assertIn("<ATTEMPT>", _normalize(dry_cmd, ATTEMPT))


class OpencodeDryRunStartParity(unittest.TestCase):
    def test_shell_command_never_leaks_the_attempt_token(self):
        dry = _opencode_args(attempt_id=None, command_attempt_id=ATTEMPT)
        start = _opencode_args(attempt_id=ATTEMPT, command_attempt_id=ATTEMPT)
        prompt_path, log_path = Path("/tmp/p.txt"), Path("/tmp/l.log")
        dry_cmd = OPENCODE.shell_command(dry, prompt_path, log_path)
        start_cmd = OPENCODE.shell_command(start, prompt_path, log_path)
        self.assertEqual(dry_cmd, start_cmd)
        self.assertNotIn(ATTEMPT, dry_cmd)
        self.assertNotIn(ATTEMPT, start_cmd)


class _IsolatedCliFixture(unittest.TestCase):
    """Full-CLI parity: compares dry-run against --register (not --start).

    --register claims the same command/prompt/log materialization path as
    --start without spawning a child process, so it is a safe stand-in for
    verifying the real wrapper end to end under isolated HOME/XDG/jobs.
    """

    def _git_repo(self, base):
        repo = base / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.email", "fixture@example.com"], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.name", "Fixture"], check=True)
        (repo / "x").write_text("x")
        subprocess.run(["git", "-C", str(repo), "add", "x"], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-qm", "init"], check=True)
        return repo

    def _isolated_env(self, base):
        # Build from scratch (no `**os.environ`): the ambient shell running
        # this suite may itself carry AGENT_*/CODEX_*/CLAUDE_* dispatch
        # variables (this attempt is itself a registered headless worker),
        # and any leak of those into the child would silently defeat the
        # isolation this regression exists to enforce.
        home = base / "home"
        home.mkdir()
        env = {}
        for key in ("PATH", "LANG", "LC_ALL", "TZ"):
            if key in os.environ:
                env[key] = os.environ[key]
        env["HOME"] = str(home)
        env["XDG_STATE_HOME"] = str(home / ".local" / "state")
        env["XDG_DATA_HOME"] = str(home / ".local" / "share")
        env["XDG_CACHE_HOME"] = str(home / ".cache")
        env["XDG_CONFIG_HOME"] = str(home / ".config")
        env["AGENT_HOME"] = str(ROOT)
        return env

    def _run_pair(self, wrapper, adapter_argv_tail, base, jobs, art, env):
        register = [
            sys.executable, str(wrapper), "--register",
            "--worktree", str(base / "repo"),
            "--capability", "autopilot-code", "--capability-mode", "dev",
            "--qa", "standard", "--intensity", "standard",
            "--attempt-id", ATTEMPT,
            "--jobs", str(jobs), "--log-dir", str(base / "logs"),
        ] + adapter_argv_tail
        dry = register.copy()
        dry[dry.index("--register")] = "--dry-run"
        env = {**env, "AGENT_ARTIFACT_ROOT": str(art), "AGENT_DISPATCH_JOBS": str(jobs)}
        dry_result = subprocess.run(dry, text=True, capture_output=True, env=env)
        self.assertEqual(dry_result.returncode, 0, dry_result.stdout + dry_result.stderr)
        register_result = subprocess.run(register, text=True, capture_output=True, env=env)
        self.assertEqual(register_result.returncode, 0, register_result.stdout + register_result.stderr)
        dry_fields = dict(line.split("=", 1) for line in dry_result.stdout.splitlines() if "=" in line)
        register_fields = dict(line.split("=", 1) for line in register_result.stdout.splitlines() if "=" in line)
        return dry_fields, register_fields


class CodexCliDryRunParity(_IsolatedCliFixture):
    def test_prompt_log_command_match_across_dry_run_and_register(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            self._git_repo(base)
            art = base / ".agent_reports"
            art.mkdir()
            jobs = base / "jobs.log"
            env = self._isolated_env(base)
            wrapper = ROOT / "adapters/codex/bin/dispatch-headless.py"
            dry_fields, register_fields = self._run_pair(
                wrapper,
                ["--slug", "codex-parity", "--sandbox", "danger-full-access", "--model", "gpt-test", "--reasoning", "low"],
                base, jobs, art, env,
            )
            self.assertEqual(dry_fields["preview"], "1")
            self.assertEqual(dry_fields["attempt_id"], "-")
            self.assertEqual(register_fields["preview"], "0")
            self.assertEqual(register_fields["attempt_id"], ATTEMPT)
            self.assertIn(ATTEMPT, dry_fields["command"])
            self.assertEqual(dry_fields["prompt_file"], register_fields["prompt_file"])
            self.assertEqual(dry_fields["log_file"], register_fields["log_file"])
            self.assertIn(ATTEMPT, dry_fields["prompt_file"])
            self.assertEqual(
                _normalize(dry_fields["command"], ATTEMPT),
                _normalize(register_fields["command"], ATTEMPT),
            )


class ClaudeCliDryRunParity(_IsolatedCliFixture):
    def test_prompt_log_command_match_across_dry_run_and_register(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            self._git_repo(base)
            art = base / ".agent_reports"
            art.mkdir()
            jobs = base / "jobs.log"
            env = self._isolated_env(base)
            wrapper = ROOT / "adapters/claude/bin/dispatch-headless.py"
            dry_fields, register_fields = self._run_pair(
                wrapper, ["--slug", "claude-parity", "--model", "claude-test", "--effort", "low"], base, jobs, art, env,
            )
            self.assertEqual(dry_fields["preview"], "1")
            self.assertEqual(dry_fields["attempt_id"], "-")
            self.assertIn(ATTEMPT, dry_fields["command"])
            self.assertEqual(dry_fields["prompt_file"], register_fields["prompt_file"])
            self.assertEqual(dry_fields["log_file"], register_fields["log_file"])
            self.assertIn(ATTEMPT, dry_fields["prompt_file"])


class OpencodeCliDryRunParity(_IsolatedCliFixture):
    def test_prompt_log_command_match_across_dry_run_and_register(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            self._git_repo(base)
            art = base / ".agent_reports"
            art.mkdir()
            jobs = base / "jobs.log"
            env = {**self._isolated_env(base), "OPENCODE_CONFIG_CONTENT": "{}"}
            wrapper = ROOT / "adapters/opencode/bin/dispatch-headless.py"
            dry_fields, register_fields = self._run_pair(
                wrapper, ["--slug", "opencode-parity", "--model", "provider/test", "--variant", "low"], base, jobs, art, env,
            )
            self.assertEqual(dry_fields["preview"], "1")
            self.assertEqual(dry_fields["attempt_id"], "-")
            self.assertIn(ATTEMPT, dry_fields["command"])
            self.assertEqual(dry_fields["prompt_file"], register_fields["prompt_file"])
            self.assertEqual(dry_fields["log_file"], register_fields["log_file"])
            self.assertIn(ATTEMPT, dry_fields["prompt_file"])


if __name__ == "__main__":
    unittest.main()
