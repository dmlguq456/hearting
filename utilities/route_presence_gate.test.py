#!/usr/bin/env python3
"""The route presence gate: one fact, three payload shapes, fail-open everywhere else."""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import importlib.util
import io
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
GATE = HERE / "route_presence_gate.py"
RESOLVER = HERE / "artifact-root.sh"
HOOK_ENTRIES = {  # what each harness actually launches
    "opencode": [str(GATE), "--opencode"],
    "codex": [str(HERE.parent / "adapters" / "codex" / "hooks" / "route-presence-gate.py")],
    "claude": [str(HERE.parent / "hooks" / "route-presence-gate.py")],
}
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
        closed = self.root / "campaigns" / "2026-10-01_demo-closed"
        closed.mkdir(parents=True)
        (closed / "campaign.json").write_text(json.dumps({
            "campaign_id": "camp_" + "1" * 32, "key": "demo-closed", "title": "Demo closed",
            "state": "abandoned", "cycles": [],
        }))
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
        return {"tool_name": tool, "session_id": sid, "cwd": str(self.tmp),
                "tool_input": {"file_path": str(path)}}

    def bash(self, command, sid=SID, cwd=None):
        return {"tool_name": "Bash", "session_id": sid, "cwd": str(cwd or self.tmp),
                # An explicit cd preserves the trigger target while exercising another worktree.
                "tool_input": {"command": f"cd {self.repo} && {command}" if cwd is None else command}}

    def judge(self, payload, harness="claude", **extra):
        return G.judge(harness, payload, {**self.env, **extra})


class CurrentWorktreeTest(Fixture):
    def test_same_worktree_edit_patch_and_commit_pass_for_all_adapters(self):
        (self.repo / 'src/engine.py').write_text('x = 2\n')
        for harness in ('claude', 'codex', 'opencode'):
            for tool in ('Edit', 'Write', 'functions.apply_patch', 'Bash'):
                args = {'file_path': str(self.repo/'src/engine.py')}
                if 'apply_patch' in tool:
                    args = {'patch': '*** Begin Patch\n*** Update File: '+str(self.repo/'src/engine.py')+'\n*** End Patch'}
                elif tool == 'Bash':
                    args = {'command': 'git commit -am fix'}
                payload = {'tool_name':tool,'tool_input':args,'session_id':SID,'cwd':str(self.repo)}
                self.assertEqual(self.judge(payload,harness=harness), '', (harness,tool))
        self.assertTrue(self.judge(self.bash('nohup python train.py &',cwd=self.repo)))


class EditTriggerTest(Fixture):
    def test_read_only_status_disables_optional_index_refresh(self):
        result = subprocess.CompletedProcess([], 0, "", "")
        with mock.patch.object(G.subprocess, "run", return_value=result) as run:
            self.assertFalse(G._has_source_change(self.repo, str(self.root), self.env))
        self.assertEqual(run.call_args.kwargs["env"]["GIT_OPTIONAL_LOCKS"], "0")

    def test_first_other_worktree_edit_is_refused_with_the_compose_line(self):
        reason = self.judge(self.edit(self.repo / "src" / "engine.py"))
        self.assertIn(f"no route for {self.root}", reason)
        line = reason.splitlines()[1]
        self.assertTrue(line.startswith("python3 "), line)
        self.assertIn("capability-route.py compose --start --shape direct --prompt-file '<task>'", line)
        self.assertNotIn("--campaign-key", line)
        self.assertNotIn("demo-closed", line)
        self.assertIn("--slug engine --cwd", line)  # the cycle folder carries the date
        self.assertIn(f"--cwd {self.repo}", line)
        self.assertIn("For one atomic, reversible inline change", reason)
        self.assertIn("make the edit in the worktree it prints", reason)
        self.assertIn("open --shape solo|staged --capability <kind> when the direction is clear", reason)
        self.assertIn("otherwise omit --shape", reason)
        self.assertIn("the frame decides the route", reason)
        self.assertIn("person-specified route", reason)
        self.assertIn("capability-route.py start --route", reason)

    def test_compose_line_can_start_unassigned_with_an_active_stream_elsewhere(self):
        other = self.root / "campaigns" / "2026-10-02_demo-other"
        other.mkdir()
        (other / "campaign.json").write_text(json.dumps({
            "campaign_id": "camp_" + "2" * 32, "key": "demo-other", "title": "Demo other",
            "state": "active", "cycles": [],
        }))
        line = self.judge(self.edit(self.repo / "src" / "engine.py")).splitlines()[1]
        self.assertIn("--unassigned", line)
        self.assertNotIn("--campaign-key", line)

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

    def test_temp_git_project_passes_from_another_cwd_too(self):
        scratch_repo = Path(tempfile.gettempdir()) / "clone"
        scratch_repo.mkdir()
        git(scratch_repo, "init", "-q")
        payload = self.edit(scratch_repo / "a.py")
        self.assertEqual(self.judge(payload), "")
        payload["cwd"] = str(scratch_repo)
        self.assertEqual(self.judge(payload), "")

    def test_codex_apply_patch_targets_are_judged(self):
        patch = "*** Begin Patch\n*** Update File: " + str(self.repo/"src/engine.py") + "\n@@\n-x = 1\n+x = 2\n*** End Patch\n"
        payload = {"tool_name": "apply_patch", "session_id": SID, "cwd": str(self.tmp),
                   "tool_input": {"command": ["apply_patch", patch]}}
        self.assertTrue(self.judge(payload, harness="codex"))
        artifact_patch = patch.replace(str(self.repo/"src/engine.py"), str(self.root/"notes.md"))
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
        with mock.patch.object(G, "_ledger_routes", side_effect=PermissionError("denied")):
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

    def test_activation_choices_agree_from_primary_and_linked_worktrees(self):
        from artifact_producer import activate, classify_root, cutover_path
        from tools.fleet.collectors import project_status

        python_bin = self.tmp / "bin"
        python_bin.mkdir()
        broken_python = python_bin / "python3"
        broken_python.write_text("#!/bin/sh\nexit 97\n", encoding="utf-8")
        broken_python.chmod(0o755)
        broken_python_path = f"{python_bin}{os.pathsep}{self.env['PATH']}"

        choices = [
            ("absent", "absent", ".agent_reports"),
            ("inactive", "absent", ".agent_reports"),
            ("active", "absent", ".agent_reports"),
            ("absent", "inactive", ".claude_reports"),
            ("absent", "active", ".claude_reports"),
            ("inactive", "inactive", ".agent_reports"),
            ("active", "inactive", ".agent_reports"),
            ("active", "active", ".agent_reports"),
            ("inactive", "active", ".claude_reports"),
            ("mismatched", "active", ".claude_reports"),
        ]
        for agent_state, claude_state, name in choices:
            with self.subTest(agent=agent_state, claude=claude_state):
                project = self.tmp / f"project-{agent_state}-{claude_state}"
                project.mkdir()
                git(project, "init", "-q")
                git(project, "commit", "--allow-empty", "-qm", "init")
                linked = project.with_name(project.name + "-linked")
                git(project, "worktree", "add", "-q", "-b", "topic", str(linked))
                # A local snapshot must not influence the primary checkout's choice.
                (linked / ".agent_reports").mkdir()

                states = {project / ".agent_reports": agent_state,
                          project / ".claude_reports": claude_state}
                for root, state in states.items():
                    if state != "absent":
                        root.mkdir()
                    if state == "inactive":
                        (root / "document.md").write_text("draft\n", encoding="utf-8")
                for index, (root, state) in enumerate(states.items(), 1):
                    if state in {"active", "mismatched"}:
                        activate(root, repository_id="repo_" + "a" * 32,
                                 artifact_root_id="root_" + str(index) * 32)
                    if state == "mismatched":
                        marker = cutover_path(root)
                        value = json.loads(marker.read_text(encoding="utf-8"))
                        value["identity"]["artifact_root_id"] = "root_" + "f" * 32
                        marker.write_text(json.dumps(value), encoding="utf-8")
                        self.assertEqual(classify_root(root)["state"], "malformed")
                modern = project / ".agent_reports"
                if agent_state == "absent" and modern.is_symlink():
                    modern.unlink()  # Model a legacy root activated before alias creation.

                expected = str(project / name)
                for checkout in (project, linked):
                    self.assertEqual(self.resolver(checkout), expected)
                    self.assertEqual(G.artifact_root(checkout, self.env), expected)
                    self.assertEqual(project_status.artifact_root(checkout),
                                     expected if Path(expected).is_dir() else None)
                    with self.subTest(checkout=checkout.name, PYTHONSAFEPATH="1"):
                        self.assertEqual(self.resolver(checkout, PYTHONSAFEPATH="1"), expected)
                    if "absent" in (agent_state, claude_state):
                        with self.subTest(checkout=checkout.name, python3="fails"):
                            self.assertEqual(self.resolver(checkout, PATH=broken_python_path), expected)

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root enters a mode-600 folder")
    def test_a_single_folder_the_shell_cannot_enter_answers_as_the_interpreter_does(self):
        # The raw answer (stdout, exit status, stderr) before any realpath, as the
        # interpreter-only resolver gave it: the physical target, refused when installed.
        env = {**self.env, "GIT_CEILING_DIRECTORIES": str(self.tmp)}
        layouts = []
        for layout in ("git", "linked", "plain"):
            project = self.tmp / f"single-{layout}"
            project.mkdir()
            start = project
            if layout != "plain":
                git(project, "init", "-q")
                git(project, "commit", "--allow-empty", "-qm", "init")
            if layout == "linked":
                start = project.with_name(project.name + "-linked")
                git(project, "worktree", "add", "-q", "-b", "topic", str(start))
            layouts.append((layout, project, start))
        refused = "artifact-root: refusing an immutable installed source tree as a writable root: {}\n"
        for layout, project, start in layouts:
            reports = project / "physical-folders" / "reports"
            release = project / "hearting" / "releases" / "v-fixture"
            cases = [("folder", None, (0, "{folder}\n", "")),
                     ("absolute-link", reports, (0, f"{reports}\n", "")),
                     ("relative-link", reports, (0, f"{reports}\n", "")),
                     ("absolute-link", release, (67, "", refused.format(release))),
                     ("relative-link", release, (67, "", refused.format(release)))]
            for name in (".agent_reports", ".claude_reports"):
                folder = project / name
                for kind, target, (code, out, err) in cases:
                    for enterable in (True, False):
                        with self.subTest(layout=layout, name=name, kind=kind,
                                          target=target and target.name, enterable=enterable):
                            entered = target or folder
                            entered.mkdir(parents=True)
                            if target is not None:
                                folder.symlink_to(target if kind == "absolute-link"
                                                  else os.path.relpath(target, project))
                            if not enterable:
                                entered.chmod(0o600)
                            try:
                                result = subprocess.run(["sh", str(RESOLVER), str(start)],
                                                        capture_output=True, text=True, env=env)
                            finally:
                                entered.chmod(0o755)
                                if target is not None:
                                    folder.unlink()
                                entered.rmdir()
                            self.assertEqual((result.returncode, result.stdout, result.stderr),
                                             (code, out.format(folder=folder), err))

    def test_linked_worktree_shares_the_primary_folder(self):
        linked = self.tmp / "work" / "proj-wt"
        git(self.repo, "worktree", "add", "-q", "-b", "feat", str(linked))
        self.ledger_line(self.root)
        self.assertEqual(self.judge(self.edit(linked / "src" / "engine.py")), "")


class SelectionImportTest(Fixture):
    """The selection module loads late, is found under PYTHONSAFEPATH=1, and an unloadable
    one is surfaced (exit 1, as its old start-up import was) only where a choice needs it."""

    UNLOADABLE = {
        "missing": "import sys\nsys.modules['artifact_root_selection'] = None\n",
        "broken": ("import importlib.abc, importlib.util, sys\n"
                   "class Broken(importlib.abc.MetaPathFinder, importlib.abc.Loader):\n"
                   "    def find_spec(self, name, path=None, target=None):\n"
                   "        if name == 'artifact_root_selection':\n"
                   "            return importlib.util.spec_from_loader(name, self)\n"
                   "    def create_module(self, spec):\n"
                   "        return None\n"
                   "    def exec_module(self, module):\n"
                   "        raise SyntaxError('invalid syntax (artifact_root_selection.py)')\n"
                   "sys.meta_path.insert(0, Broken())\n"),
        "empty": ("import sys, types\n"
                  "sys.modules['artifact_root_selection'] = types.ModuleType('artifact_root_selection')\n"),
    }

    def unloadable(self, how):
        """Environment whose Python processes cannot load the selection module."""
        site = self.tmp / f"site-{how}"
        site.mkdir(exist_ok=True)
        (site / "sitecustomize.py").write_text(self.UNLOADABLE[how], encoding="utf-8")
        return {"PYTHONPATH": str(site)}

    def stripped(self, how):
        """Hook entries whose own ``artifact_root_selection.py`` is missing, broken or empty, so
        the shell resolver's interpreter fails on it (exit 2 / 1 / 0 with no answer) as such an
        install would."""
        tree = self.tmp / f"tree-{how}"
        for relative in ("utilities/route_presence_gate.py", "utilities/artifact-root.sh",
                         "hooks/route-presence-gate.py",
                         "adapters/codex/hooks/route-presence-gate.py"):
            (tree / relative).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(HERE.parent / relative, tree / relative)
        (tree / "tools").symlink_to(HERE.parent / "tools")
        if how in ("broken", "empty"):
            (tree / "utilities" / "artifact_root_selection.py").write_text(
                "def select_artifact_root(:\n" if how == "broken" else "", encoding="utf-8")
        return tree

    def shell_project(self, name):
        """A separate-git-dir checkout (the shell resolver answers) and the folder it looks in."""
        top = self.tmp / "work" / name
        git(self.tmp, "init", "-q", f"--separate-git-dir={self.tmp / f'{name}.git'}", str(top))
        (top / "src").mkdir()
        (top / "src" / "engine.py").write_text("x = 1\n")
        return top, Path(self.resolve(top)).parent

    def resolve(self, top):
        return os.path.realpath(subprocess.run(
            ["sh", str(RESOLVER), str(top)], capture_output=True, text=True, check=True,
            env=self.env).stdout.strip())

    def hook(self, harness, target, cwd, tree=None, **extra):
        entry = HOOK_ENTRIES[harness]
        if tree is not None:
            entry = [str(tree / Path(entry[0]).relative_to(HERE.parent)), *entry[1:]]
        sid = "ses_gate" if harness == "opencode" else SID
        if harness == "opencode":
            payload = {"tool": "write", "args": {"filePath": str(target)}, "sessionID": sid,
                       "cwd": str(cwd)}
        elif harness == "codex":
            patch = f"*** Begin Patch\n*** Update File: {target}\n@@\n-x = 1\n+x = 2\n*** End Patch\n"
            payload = {"tool_name": "apply_patch", "session_id": sid, "cwd": str(cwd),
                       "tool_input": {"command": ["apply_patch", patch]}}
        else:
            payload = {"tool_name": "Edit", "session_id": sid, "cwd": str(cwd),
                       "tool_input": {"file_path": str(target)}}
        result = subprocess.run([sys.executable, *entry], input=json.dumps(payload),
                                capture_output=True, text=True, cwd=str(cwd),
                                env={**self.env, "PYTHONDONTWRITEBYTECODE": "1", **extra})
        return self.outcome(harness, result)

    @staticmethod
    def outcome(harness, result):
        code, out, err = result.returncode, result.stdout, result.stderr
        if code == 1 and not out and "artifact_root_selection" in err:
            return "surfaced"
        if code == 0 and not out and not err:
            return "passed"
        reason = {"opencode": out if code == 1 else "", "claude": err if code == 2 else ""}.get(harness)
        if harness == "codex" and code == 0 and out:
            decision = json.loads(out)
            reason = decision["reason"] if decision.get("decision") == "block" else ""
        match = re.search(r"no route for (\S+) yet", reason or "")
        return f"refused:{match.group(1)}" if match else f"other:{code}:{out!r}:{err[-300:]!r}"

    def test_importing_the_gate_leaves_the_selection_module_unloaded(self):
        probe = ("import sys; sys.path.insert(0, sys.argv[1]); import route_presence_gate; "
                 "print('artifact_root_selection' in sys.modules)")
        result = subprocess.run([sys.executable, "-c", probe, str(HERE)], capture_output=True,
                                text=True, check=True,
                                env={**self.env, "PYTHONDONTWRITEBYTECODE": "1"})
        self.assertEqual(result.stdout.strip(), "False")

    def test_hook_entries_against_the_start_up_import_they_replace(self):
        linked = self.tmp / "work" / "proj-wt"
        git(self.repo, "worktree", "add", "-q", "-b", "feat", str(linked))
        target = linked / "src" / "engine.py"
        override = self.tmp / "override"
        override.mkdir()
        release = self.tmp / "hearting" / "releases" / "v-fixture"
        release.mkdir(parents=True)
        # No standard .git: the shell resolver answers, in the folder it looks in (``base``).
        separate, _ = self.shell_project("separate")
        two, base = self.shell_project("separate-two")
        (base / ".agent_reports").mkdir()
        (base / ".claude_reports").mkdir()
        single, base = self.shell_project("separate-single")
        (base / ".agent_reports").symlink_to(release)  # refused at once (67)
        chosen, base = self.shell_project("separate-chosen")
        (base / ".agent_reports").symlink_to(release)  # chosen by python3, then refused (67)
        (base / ".claude_reports").mkdir()
        mine, other, two_root = str(self.root), self.resolve(separate), self.resolve(two)
        missing, broken = self.unloadable("missing"), self.unloadable("broken")
        gone, unparsable = self.stripped("missing"), self.stripped("broken")
        empty, emptied = self.unloadable("empty"), self.stripped("empty")
        # The shell's own answer for the last row: exit 0 without a path.
        shell = [str(emptied / "utilities" / "artifact-root.sh"), str(two)]
        answer = subprocess.run(["sh", *shell], capture_output=True, text=True, env=self.env)
        self.assertEqual((answer.returncode, answer.stdout), (0, "\n"))
        safe = {"PYTHONSAFEPATH": "1", "AGENT_DISPATCH_DEPTH": "-1"}
        relative, parentless = "relative", str(self.tmp / "absent" / "root")
        rows = [
            ("needs a route", target, self.tmp, {}, None, f"refused:{mine}"),
            ("current worktree", target, linked, {}, None, "passed"),
            ("explicit root", target, self.tmp, {"AGENT_ARTIFACT_ROOT": str(override)}, None,
             f"refused:{override}"),
            ("shell resolver", separate / "src" / "engine.py", self.tmp, {}, None,
             f"refused:{other}"),
            ("shell resolver, two folders", two / "src" / "engine.py", self.tmp, {}, None,
             f"refused:{two_root}"),
            ("PYTHONSAFEPATH=1, depth -1", target, self.tmp, safe, None, f"refused:{mine}"),
            ("selection missing", target, self.tmp, missing, None, "surfaced"),
            ("selection broken", target, self.tmp, broken, None, "surfaced"),
            ("selection missing, current worktree", target, linked, missing, None, "passed"),
            ("selection missing, explicit root", target, self.tmp,
             {**missing, "AGENT_ARTIFACT_ROOT": str(override)}, None, f"refused:{override}"),
            # The shell's own refusals need no choice: no module is asked for (64, 66, 67).
            ("selection missing, relative root (64)", target, self.tmp,
             {**missing, "AGENT_ARTIFACT_ROOT": relative}, None, "passed"),
            ("selection missing, root without a parent (66)", target, self.tmp,
             {**missing, "AGENT_ARTIFACT_ROOT": parentless}, None, "passed"),
            ("selection missing, single link to a release (67)", single / "src" / "engine.py",
             self.tmp, missing, None, "passed"),
            ("selection missing, chosen link to a release (67)", chosen / "src" / "engine.py",
             self.tmp, missing, None, "passed"),
            # The same with the file itself gone, and where the shell's choice fails on it.
            ("selection file gone, relative root (64)", target, self.tmp,
             {**missing, "AGENT_ARTIFACT_ROOT": relative}, gone, "passed"),
            ("selection file gone, root without a parent (66)", target, self.tmp,
             {**missing, "AGENT_ARTIFACT_ROOT": parentless}, gone, "passed"),
            ("selection file gone, single link to a release (67)", single / "src" / "engine.py",
             self.tmp, missing, gone, "passed"),
            ("selection file gone, two folders (2)", two / "src" / "engine.py", self.tmp,
             missing, gone, "surfaced"),
            ("selection file broken, two folders (1)", two / "src" / "engine.py", self.tmp,
             broken, unparsable, "surfaced"),
            # No path and no refusal from the shell: the module is probed as well. A shell
            # ended by a signal is test_only_the_shells_refusals_skip_the_probe's -15.
            ("selection file empty, two folders (0, no path)", two / "src" / "engine.py",
             self.tmp, empty, emptied, "surfaced"),
        ]
        for harness in HOOK_ENTRIES:
            for label, path, cwd, extra, tree, expected in rows:
                with self.subTest(harness=harness, row=label):
                    self.assertEqual(self.hook(harness, path, cwd, tree, **extra), expected)
        for harness in HOOK_ENTRIES:
            self.ledger_line(self.root, harness=harness,
                             sid="ses_gate" if harness == "opencode" else SID)
            with self.subTest(harness=harness, row="route held"):
                self.assertEqual(self.hook(harness, target, self.tmp), "passed")

    def test_only_the_selection_import_escapes_the_fail_open_judgement(self):
        linked = self.tmp / "work" / "proj-wt"
        git(self.repo, "worktree", "add", "-q", "-b", "feat", str(linked))
        payload = self.edit(linked / "src" / "engine.py")
        with mock.patch.dict(sys.modules, {"artifact_root_selection": None}):
            with self.assertRaises(G.SelectionUnavailable):
                self.judge(payload)
            # The shell's own refusals (64, 66) involve no choice, so no module is asked for.
            self.assertEqual(self.judge(payload, AGENT_ARTIFACT_ROOT="relative"), "")
            self.assertEqual(self.judge(payload, AGENT_ARTIFACT_ROOT=str(self.tmp / "a" / "b")), "")
            self.assertEqual(self.judge({**payload, "cwd": str(linked)}), "")
        self.assertEqual(self.judge(payload, AGENT_ARTIFACT_ROOT="relative"), "")
        with mock.patch.object(G, "_route_chain", side_effect=ImportError("gone")):
            self.assertEqual(self.judge(payload), "")

    def test_only_the_shells_refusals_skip_the_probe(self):
        env = {**self.env, "AGENT_ARTIFACT_ROOT": "/answered/by/the/shell"}
        answers = {64: "", 66: "", 67: "", 1: "", 2: "", 0: "\n", -signal.SIGTERM: ""}
        with mock.patch.dict(sys.modules, {"artifact_root_selection": None}):
            for code, stdout in answers.items():
                answer = subprocess.CompletedProcess([], code, stdout=stdout, stderr="")
                with self.subTest(returncode=code), \
                        mock.patch.object(G.subprocess, "run", return_value=answer):
                    if code in (64, 66, 67):
                        self.assertEqual(G.artifact_root(self.repo, env), "")
                    else:
                        self.assertRaises(G.SelectionUnavailable, G.artifact_root, self.repo, env)


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
                   "sessionID": "ses_abc", "cwd": str(self.tmp)}
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
