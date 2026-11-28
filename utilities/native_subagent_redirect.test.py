#!/usr/bin/env python3
"""Whether Hearting takes a person's native subagent call is decided once, before anything is registered;
once a start receipt says Hearting holds it, the native call is refused."""
from __future__ import annotations
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])

import io
import json
from pathlib import Path
import shutil
import signal
import sqlite3
import subprocess
import tempfile
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
import native_subagent_redirect as redirect  # noqa: E402
import session_identity  # noqa: E402

SID = "native-subagent-session"
WORKER_ENV = ("AGENT_SESSION_ROLE", "AGENT_DISPATCH_CHILD", "AGENT_DISPATCH_DEPTH", "FLEET_TITLE_REFRESH", "MEM_DISTILL")


def claude(**args):
    return {"session_id": SID, "hook_event_name": "PreToolUse", "tool_name": "Agent", "cwd": os.getcwd(),
            "tool_input": {"description": "Find the panel rule", "prompt": "Where is the panel rule?", **args}}


def codex(tool="spawn_agent", **args):
    return {"session_id": SID, "hook_event_name": "PreToolUse", "tool_name": tool, "cwd": os.getcwd(),
            "tool_input": {"message": "Where is the panel rule?", **args}}


def ours(harness):
    """The registration this release ships for the hook (installed as it is)."""
    path, tool = redirect._REGISTRATION[harness]
    groups = json.loads((ROOT / "adapters" / harness / path).read_text())["hooks"]["PreToolUse"]
    [group] = [g for g in groups if tool in g["matcher"].split("|")]
    return group


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:   # a zombie no one has reaped yet has ended too
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] != "Z"
    except (OSError, IndexError):
        return True


def gone(pid, seconds=15.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not alive(pid):
            return True
        time.sleep(0.05)
    return False


# A stand-in for `capability-route.py`: it records what the hook gave it (argv, the brief, the session identity)
# and answers the way FAKE_MODE names.
FAKE_ROUTE = r'''
import json, os, sys, time, subprocess
mode = os.environ.get("FAKE_MODE", "running")
base = os.environ["FAKE_RECORD"]
open(base + ".pid", "w").write(str(os.getpid()))
brief = "" if mode == "no-read" else sys.stdin.read()
record = {"argv": sys.argv[1:], "brief": brief,
          "env": {k: os.environ.get(k) for k in ("CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID", "OPENCODE_SESSION_ID",
                                                  "AGENT_DISPATCH_CALLER_HARNESS", "AGENT_CAPACITY_RESUME_CHAIN")}}
with open(base, "a") as fh:
    fh.write(json.dumps(record) + "\n")

def say(**receipt):
    print(json.dumps({"route_id": "rt-0123456789abcdef", **receipt}), flush=True)

if mode in ("running", "no-read"):
    say(state="running", unit_attempt_id="att-x", parent_next="end-turn")
elif mode == "linger":
    # The receipt is written; the bookkeeping after it is slow. The hook returns and leaves it running.
    say(state="running", parent_next="end-turn")
    time.sleep(30)
elif mode == "late-grandchild":
    # A worker the start launched keeps running (and the output files open) after the start returns.
    late = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(3)"], start_new_session=True)
    open(base + ".late", "w").write(str(late.pid))
    say(state="running", parent_next="end-turn")
elif mode == "capacity":
    # A full worker slot: the runtime's capacity resume is armed and its notice brings the answer later.
    say(state="waiting-capacity", reason="launch-capacity-wait", required_action="wait-for-auto-resume",
        parent_next="end-turn", auto_resume={"state": "armed", "cause": "launch-capacity"})
elif mode == "running-then-fails":
    say(state="running", parent_next="end-turn")
    raise SystemExit(1)
elif mode == "registered-then-raised":
    # The start raised after the adapter claimed a row: the receipt keeps it (work_start, registered_attempts).
    say(state="needs-attention", reason="OSError", registered_attempts=["att-r"], parent_next="end-turn")
elif mode == "ended":
    say(state="needs-attention", reason="unit-outcome-needs-inspection", result={"classification": "missing"},
        resume_command="cr start --route r", parent_next="compose", parent_next_command="cr compose ...")
elif mode == "not-admitted":
    say(state="needs-attention", reason="unit-launch-not-admitted", unit_attempt_id="att-n",
        required_action="resume-later")
elif mode == "refused":
    print("capability-route: quick-headless-unavailable", file=sys.stderr)
    raise SystemExit(64)
elif mode == "garbage":
    print("not json")
'''


class Base(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ, {key: "" for key in WORKER_ENV})
        patcher.start()
        self.addCleanup(patcher.stop)
        for key in WORKER_ENV:
            os.environ.pop(key, None)


class Homes(Base):
    """Claude and Codex homes and a project folder with nothing in them but what a test puts there, run by a
    harness started with no arguments."""

    def setUp(self):
        super().setUp()
        self.tmp = Path(tempfile.mkdtemp(prefix="nsr-home-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.claude_home, self.codex_home = self.tmp / "claude", self.tmp / "codex"
        self.project = self.tmp / "project" / "sub"
        for folder in (self.claude_home, self.codex_home, self.project):
            folder.mkdir(parents=True)
        for patcher in (mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.claude_home),
                                                     "CODEX_HOME": str(self.codex_home), "CLAUDE_PROJECT_DIR": ""}),
                        mock.patch.object(redirect, "_harness_argv", return_value=["harness"]),
                        mock.patch.dict(redirect.MANAGED, {"claude": (self.tmp / "managed",),
                                                           "codex": (self.tmp / "system",)})):
            patcher.start()
            self.addCleanup(patcher.stop)

    def write(self, path, text):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def ship(self, harness, name):
        suffix = redirect._SUFFIX[harness]
        home = self.claude_home if harness == "claude" else self.codex_home
        return self.write(home / "agents" / (name + suffix),
                          (ROOT / "adapters" / harness / "agents" / (name + suffix)).read_text())

    def register(self, harness, *extra, path=None):
        """The installed registration (exactly once), and any other `PreToolUse` groups."""
        path = path or (self.claude_home / "settings.json" if harness == "claude" else self.codex_home / "hooks.json")
        self.write(path, json.dumps({"hooks": {"PreToolUse": [ours(harness), *extra]}}))


class NameTableTest(Base):
    def depth(self, harness, payload):
        return redirect.depth_for(harness, redirect.normalized(harness, payload))

    def test_each_name_of_the_table_picks_its_depth(self):
        table = {"light": "light", "Explore": "light", "explorer": "light", "explore": "light", "memory-scout": "light",
                 "balanced": "balanced", "worker": "balanced", "default": "balanced",
                 "general-purpose": "balanced-deep", "Plan": "balanced-deep", "deep": "deep"}
        for name, depth in table.items():
            self.assertEqual(self.depth("claude", claude(subagent_type=name)), depth, name)
            self.assertEqual(self.depth("codex", codex(agent_type=name)), depth, name)
        self.assertEqual(self.depth("claude", claude()), "balanced-deep")
        self.assertEqual(self.depth("codex", codex(fork_context=False)), "balanced")

    def test_forms_the_brief_cannot_carry_are_left_alone(self):
        for harness, payload in (
                ("claude", claude(subagent_type="general")),
                ("claude", claude(subagent_type="fork")),
                ("claude", claude(subagent_type="web-fetch")),
                ("claude", claude(subagent_type="my-plugin:reviewer")),
                ("claude", claude(subagent_type="light", isolation="worktree")),
                ("claude", claude(subagent_type="light", resume="agent-1")),
                ("claude", claude(subagent_type="light", model="sonnet")),     # a model the call names stays native
                ("claude", claude(subagent_type="light", team_name="t")),       # an argument the table does not know
                ("claude", claude(subagent_type="light", prompt="")),
                ("claude", {**claude(subagent_type="light"), "session_id": ""}),
                ("claude", {**claude(subagent_type="light"), "tool_name": "Bash"}),
                ("codex", codex(agent_type="light", fork_context=True)),
                ("codex", codex(agent_type=["light"])),                          # not a name
                ("codex", codex(agent_type="light", model="gpt-6-luna")),
                ("codex", codex(agent_type="light", reasoning_effort="high")),
                ("codex", {**codex(agent_type="light"), "tool_input": {"items": [{"type": "text", "text": "x"}]}}),
                ("codex", codex(agent_type="light", items=[{"type": "text", "text": "x"}])),   # message and items
                ("codex", codex(agent_type="light", message=["not", "text"]))):
            with self.subTest(harness=harness, payload=payload):
                with self.assertRaises(redirect.Skip):
                    self.depth(harness, payload)

    def test_a_codex_v2_call_is_left_native(self):
        # Only the verified v1 `spawn_agent` form is taken; v2 validates its own arguments after the hook.
        for payload in (codex("collaborationspawn_agent", agent_type="explorer", task_name="look", fork_turns="none"),
                        codex("collaborationspawn_agent", agent_type="explorer", fork_turns="none"),   # no task_name
                        codex(agent_type="explorer", task_name="look", fork_turns="none"),
                        codex(agent_type="explorer", fork_turns="none")):
            with self.subTest(payload=payload), self.assertRaises(redirect.Skip):
                redirect.normalized("codex", payload)
        registered = [g for g in json.loads((ROOT / "adapters/codex/hooks/hooks.json").read_text())["hooks"]["PreToolUse"]
                      if "pretooluse-subagent-redirect.py" in g["hooks"][0]["command"]]
        self.assertEqual([g["matcher"] for g in registered], ["spawn_agent"])    # v2's tool runs no hook of this

    def test_opencode_is_not_intercepted(self):
        payload = {"tool": "subagent", "sessionID": SID, "args": {"agent": "explore", "prompt": "x"}}
        with mock.patch.object(redirect, "start") as start:
            self.assertIsNone(redirect.redirect("opencode", payload))
        start.assert_not_called()
        self.assertEqual(redirect.main(["--opencode"], io.StringIO(json.dumps(payload))), 0)

    def test_the_brief_is_the_title_and_the_prompt(self):
        call = redirect.normalized("claude", claude(subagent_type="light"))
        self.assertEqual(redirect.brief(call), "Find the panel rule\n\nWhere is the panel rule?\n")
        call = redirect.normalized("codex", codex(agent_type="light"))
        self.assertEqual(redirect.brief(call), "Where is the panel rule?\n")


class DefinitionTest(Homes):
    """The definition the harness picks for the name, compared whole (review R2 🔴6)."""

    def check(self, harness, name):
        try:
            redirect.require_shipped_definition(harness, name, str(self.project))
            return True
        except Exception:  # noqa: BLE001 -- the redirect lets the native call run on any doubt
            return False

    def test_claude_hearting_definitions_shipped_linked_or_rendered(self):
        for name in ("light", "deep", "general-purpose"):
            self.ship("claude", name)                    # deep's description mentions light: it is deep's own file
        self.assertTrue(self.check("claude", "light"))
        (self.claude_home / "agents" / "balanced.md").symlink_to(ROOT / "adapters/claude/agents/balanced.md")
        self.assertTrue(self.check("claude", "balanced"))
        # Activation renders the user's own model pin into the file; the rest is Hearting's text.
        shipped = (ROOT / "adapters/claude/agents/deep.md").read_text()
        self.write(self.claude_home / "agents/deep.md", shipped.replace("\nmodel: opus\n", "\nmodel: haiku\n")
                   .replace("\neffort: xhigh\n", "\neffort: low\n").replace("(tier deep)", "(tier light)"))
        self.assertTrue(self.check("claude", "deep"))
        self.assertFalse(self.check("claude", "memory-scout"))             # no definition at all: not certain

    def test_claude_a_changed_frontmatter_or_body_is_not_hearting_s(self):
        shipped = (ROOT / "adapters/claude/agents/light.md").read_text()
        for changed in (shipped.replace("\nmodel: sonnet\n", "\nmodel: sonnet\nisolation: worktree\n"),  # a field
                        shipped.replace("\nname: light\n", "\nname: light # local choice\n"),           # a comment
                        shipped.replace("\nname: light\n", "\nname: 'light'\n"),
                        shipped + "\nAlso keep notes.\n"):
            with self.subTest(changed=changed[:120]):
                self.write(self.claude_home / "agents/light.md", changed)
                self.assertFalse(self.check("claude", "light"))

    def test_claude_another_definition_of_the_name_in_any_form(self):
        self.ship("claude", "light")
        for text in ("---\nname: light\ndescription: The project's own\n---\nProject prompt.\n",
                     "---\nname: light # local choice\ndescription: x\n---\nMine.\n",
                     "---\n\"name\": \"light\"\ndescription: x\n---\nMine.\n",
                     "---\nname: >-\n  light\ndescription: x\n---\nMine.\n"):
            with self.subTest(text=text):
                self.write(self.project / ".claude/agents/reviewer.md", text)
                self.assertFalse(self.check("claude", "light"))
                self.write(self.claude_home / "agents/mine.md", text)      # the user's own, beside Hearting's
                (self.project / ".claude/agents/reviewer.md").unlink()
                self.assertFalse(self.check("claude", "light"))
                (self.claude_home / "agents/mine.md").unlink()
        self.assertTrue(self.check("claude", "light"))

    def test_a_managed_or_system_definition_comes_first(self):
        self.ship("claude", "light")
        self.ship("codex", "light")
        self.write(self.tmp / "managed/.claude/agents/team.md", "---\nname: light\ndescription: managed\n---\nx\n")
        self.assertFalse(self.check("claude", "light"))
        self.write(self.tmp / "system/agents/team.toml", 'name = "light"\ndescription = "system"\n')
        self.assertFalse(self.check("codex", "light"))
        self.write(self.tmp / "managed/managed-settings.json", json.dumps({"permissions": {"deny": ["Agent"]}}))
        self.register("claude")
        with mock.patch.object(redirect, "start") as start:
            self.assertFalse(OtherDecisionTest.sole(self, "claude", "Agent"))
        start.assert_not_called()

    def test_claude_built_ins_unless_some_scope_defines_the_name(self):
        for name in ("Explore", "Plan", "general-purpose"):
            self.assertTrue(self.check("claude", name), name)
        self.write(self.project.parent / ".claude/agents/scouts/mine.md", "---\nname: Explore\n---\nMine.\n")
        self.assertFalse(self.check("claude", "Explore"))                 # a project agent walking up wins
        other = self.tmp / "elsewhere"
        self.write(other / ".claude/agents/x.md", "---\nname: Plan\n---\nx\n")
        with mock.patch.dict(os.environ, {"CLAUDE_PROJECT_DIR": str(other)}):
            self.assertFalse(self.check("claude", "Plan"))
        (self.claude_home / "agents/bad.md").parent.mkdir(parents=True, exist_ok=True)
        (self.claude_home / "agents/bad.md").write_bytes(b"---\nname: \xff\n---\n")
        self.assertFalse(self.check("claude", "general-purpose"))          # unreadable: not certain

    def test_codex_roles_by_their_name_and_their_options(self):
        self.ship("codex", "light")
        self.assertTrue(self.check("codex", "light"))
        for name in ("default", "worker", "explorer"):
            self.assertTrue(self.check("codex", name), name)
        shipped = (ROOT / "adapters/codex/agents/light.toml").read_text()
        self.write(self.codex_home / "agents/light.toml", shipped.replace('model = "gpt-6-luna"', 'model = "x"'))
        self.assertTrue(self.check("codex", "light"))                     # the rendered model line
        self.write(self.codex_home / "agents/light.toml",
                   shipped.replace('sandbox_mode = "workspace-write"', 'sandbox_mode = "danger-full-access"'))
        self.assertFalse(self.check("codex", "light"))                    # a role option the user changed
        self.ship("codex", "light")
        self.write(self.codex_home / "agents/scout.toml", 'name = "explorer"\ndescription = "mine"\n')
        self.assertFalse(self.check("codex", "explorer"))                 # a user role overrides the built-in
        self.write(self.project / ".codex/agents/x.toml", 'name = "light"\ndescription = "p"\n')
        self.assertFalse(self.check("codex", "light"))                    # a project layer comes first

    def test_codex_a_declared_role_or_the_file_it_names(self):
        self.write(self.codex_home / "config.toml", '[agents]\ndefault_subagent_model = "x"\n[agents.worker]\n'
                   'description = "mine"\n')
        self.assertFalse(self.check("codex", "worker"))                   # a declared role
        self.assertTrue(self.check("codex", "default"))                   # the installer's own `[agents]` keys
        self.write(self.codex_home / "private/role.toml", 'name = "explorer"\ndescription = "hidden"\n')
        self.write(self.codex_home / "config.toml", '[agents.team_scout]\nconfig_file = "private/role.toml"\n')
        self.assertFalse(self.check("codex", "explorer"))                 # its config_file names it
        self.assertTrue(self.check("codex", "default"))
        self.write(self.codex_home / "config.toml", '[agents.team_scout]\nconfig_file = "private/missing.toml"\n')
        self.assertFalse(self.check("codex", "default"))                  # a file that cannot be read

    def test_the_redirect_starts_nothing_for_a_collision(self):
        self.ship("claude", "light")
        self.write(self.project / ".claude/agents/light.md", "---\nname: light\n---\nMine.\n")
        with mock.patch.object(redirect, "start") as start, \
                mock.patch.object(session_identity, "known_sub_session_root", return_value=SID):
            self.assertIsNone(redirect.redirect("claude", {**claude(subagent_type="light"), "cwd": str(self.project)}))
        start.assert_not_called()


class OtherDecisionTest(Homes):
    """Another decision on the same call, visible or not (review R2 🔴5)."""

    REFUSE = {"type": "command", "command": "my-refusal"}

    def sole(self, harness, tool):
        try:
            redirect.require_sole_decision(harness, tool, str(self.project))
            return True
        except Exception:  # noqa: BLE001 -- the redirect lets the native call run on any doubt
            return False

    def test_claude_only_this_hook_registered_once(self):
        self.register("claude", {"matcher": "Bash", "hooks": [{"type": "command", "command": "guard"}]})
        self.assertTrue(self.sole("claude", "Agent"))

    def test_claude_another_matching_hook_or_rule(self):
        cases = [
            ({"matcher": "Agent", "hooks": [self.REFUSE]}, {}),
            ({"matcher": "*", "hooks": [self.REFUSE]}, {}),
            ({"hooks": [self.REFUSE]}, {}),
            ({"matcher": "^Ag", "hooks": [self.REFUSE]}, {}),
            ({"matcher": "Task", "hooks": [self.REFUSE]}, {}),
            (None, {"permissions": {"deny": ["Agent(Explore)"]}}),
            (None, {"permissions": {"ask": ["Task"]}}),
        ]
        for group, extra in cases:
            with self.subTest(group=group, extra=extra):
                self.write(self.claude_home / "settings.json", json.dumps(
                    {"hooks": {"PreToolUse": [ours("claude"), *([group] if group else [])]}, **extra}))
                self.assertFalse(self.sole("claude", "Agent"))
        self.register("claude")
        self.write(self.project.parent / ".claude/settings.local.json",
                   json.dumps({"hooks": {"PreToolUse": [{"matcher": "Agent", "hooks": [self.REFUSE]}]}}))
        self.assertFalse(self.sole("claude", "Agent"))

    def test_our_hook_is_known_by_its_exact_command(self):
        # A command that only contains the hook's file name is another hook (a wrapper that may refuse).
        group = ours("claude")
        wrapped = {**group, "hooks": [{**group["hooks"][0], "command": "my-wrap " + group["hooks"][0]["command"]}]}
        self.write(self.claude_home / "settings.json", json.dumps({"hooks": {"PreToolUse": [wrapped]}}))
        self.assertFalse(self.sole("claude", "Agent"))
        old = {**group, "hooks": [{**group["hooks"][0], "command": group["hooks"][0]["command"].replace(" --redirect", "")}]}
        self.write(self.claude_home / "settings.json", json.dumps({"hooks": {"PreToolUse": [ours("claude"), old]}}))
        self.assertFalse(self.sole("claude", "Agent"))                    # the old line beside the new one

    def test_registered_twice_or_not_seen_at_all(self):
        self.register("claude")
        self.register("claude", path=self.project / ".claude/settings.json")
        self.assertFalse(self.sole("claude", "Agent"))                    # two layers: it may run twice
        (self.project / ".claude/settings.json").unlink()
        self.assertTrue(self.sole("claude", "Agent"))
        (self.claude_home / "settings.json").unlink()
        self.assertFalse(self.sole("claude", "Agent"))                    # registered where no file shows it
        self.register("codex")
        self.write(self.codex_home / "config.toml", '[[hooks.PreToolUse]]\nmatcher = "spawn_agent"\n'
                   '[[hooks.PreToolUse.hooks]]\ntype = "command"\n'
                   f'command = {json.dumps(ours("codex")["hooks"][0]["command"])}\n')
        self.assertFalse(self.sole("codex", "spawn_agent"))

    def test_a_layer_given_on_the_harness_command_line(self):
        self.register("claude")
        self.register("codex")
        for harness, tool, argv in (
                ("claude", "Agent", ["claude", "--settings", "/x/settings.json"]),
                ("claude", "Agent", ["node", "/x/cli.js", "--agents={\"light\":{}}"]),
                ("claude", "Agent", ["claude", "--add-dir", "/x"]),
                ("claude", "Agent", ["claude", "--plugin-dir", "/x"]),
                ("claude", "Agent", ["claude", "--disallowedTools", "Agent"]),
                ("codex", "spawn_agent", ["codex", "-c", "hooks.x=1"]),
                ("codex", "spawn_agent", ["codex", "-chooks.x=1"]),
                ("codex", "spawn_agent", ["codex", "--config=hooks.x=1"]),
                ("codex", "spawn_agent", ["codex", "--profile", "work"])):
            with self.subTest(argv=argv), mock.patch.object(redirect, "_harness_argv", return_value=argv):
                self.assertFalse(self.sole(harness, tool))
        for harness, tool, argv in (("claude", "Agent", ["claude", "--model", "x", "-p", "hello --settings"]),
                                    ("codex", "spawn_agent", ["codex", "--no-alt-screen"])):
            with self.subTest(argv=argv), mock.patch.object(redirect, "_harness_argv", return_value=argv):
                self.assertTrue(self.sole(harness, tool))
        with mock.patch.object(redirect, "_harness_argv", side_effect=OSError("no /proc")):
            self.assertFalse(self.sole("claude", "Agent"))                # a command line that cannot be read

    def test_the_harness_is_the_nearest_process_that_is_no_shell(self):
        # `timeout` stands for the harness: it runs a shell, which runs the hook's Python.
        script = ("import json, sys; sys.path.insert(0, sys.argv[1]); import native_subagent_redirect as r; "
                  "print(json.dumps(r._harness_argv()))")
        done = subprocess.run(["timeout", "60", "sh", "-c", '"$2" -c "$0" "$1"; true', script,
                               str(ROOT / "utilities"), sys.executable], capture_output=True, text=True, timeout=90)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(json.loads(done.stdout)[:3], ["timeout", "60", "sh"])

    def test_codex_layers_plugins_and_a_linked_worktree_s_main_checkout(self):
        self.register("codex")
        self.assertTrue(self.sole("codex", "spawn_agent"))
        refuse = {"matcher": "spawn_agent", "hooks": [self.REFUSE]}
        self.write(self.project / ".codex/hooks.json", json.dumps({"hooks": {"PreToolUse": [refuse]}}))
        self.assertFalse(self.sole("codex", "spawn_agent"))
        shutil.rmtree(self.project / ".codex")
        self.write(self.codex_home / "config.toml", '[[hooks.PreToolUse]]\nmatcher = ".*agent"\n'
                   '[[hooks.PreToolUse.hooks]]\ntype = "command"\ncommand = "my-refusal"\n')
        self.assertFalse(self.sole("codex", "spawn_agent"))
        (self.codex_home / "config.toml").unlink()
        self.write(self.codex_home / "plugins/cache/m/p/1/hooks/hooks.json",
                   json.dumps({"hooks": {"PreToolUse": [{"matcher": "*", "hooks": [{"command": "x"}]}]}}))
        self.assertFalse(self.sole("codex", "spawn_agent"))
        shutil.rmtree(self.codex_home / "plugins")
        # A linked worktree: Codex also reads the main checkout's project layer.
        main, linked = self.tmp / "main", self.tmp / "wt" / "linked"
        (main / ".git" / "worktrees" / "linked").mkdir(parents=True)
        (main / ".git" / "worktrees" / "linked" / "commondir").write_text("../..\n")
        linked.mkdir(parents=True)
        (linked / ".git").write_text(f"gitdir: {main / '.git' / 'worktrees' / 'linked'}\n")
        self.assertTrue(redirect.require_sole_decision("codex", "spawn_agent", str(linked)) is None)
        self.write(main / ".codex/hooks.json", json.dumps({"hooks": {"PreToolUse": [refuse]}}))
        with self.assertRaises(redirect.Skip):
            redirect.require_sole_decision("codex", "spawn_agent", str(linked))
        (linked / ".git").write_text("not a gitdir line\n")
        with self.assertRaises(Exception):
            redirect.require_sole_decision("codex", "spawn_agent", str(linked))   # cannot tell: native

    def test_codex_another_hook_on_the_alias_agent(self):
        # Codex picks `spawn_agent` hooks by its alias `Agent` too (core/src/tools/hook_names.rs).
        for matcher in ("Agent", "Agent|Task", "^Agent$"):
            with self.subTest(matcher=matcher):
                self.register("codex", {"matcher": matcher, "hooks": [self.REFUSE]})
                self.assertFalse(self.sole("codex", "spawn_agent"))
        self.register("codex", {"matcher": "Bash", "hooks": [self.REFUSE]})
        self.assertTrue(self.sole("codex", "spawn_agent"))

    def test_codex_our_hook_registered_again_on_the_alias_agent(self):
        self.register("codex", {**ours("codex"), "matcher": "Agent"})
        self.assertFalse(self.sole("codex", "spawn_agent"))              # it would run twice

    def test_claude_server_managed_settings(self):
        self.register("claude")
        self.write(self.claude_home / "remote-settings.json", "{}")       # the copy the server's settings leave
        self.assertFalse(self.sole("claude", "Agent"))
        (self.claude_home / "remote-settings.json").unlink()
        self.assertTrue(self.sole("claude", "Agent"))

    def test_a_call_another_hook_refuses_starts_nothing(self):
        self.ship("claude", "light")
        self.register("claude", {"matcher": "Agent", "hooks": [{"type": "command", "command": "exit 2"}]})
        with mock.patch.object(redirect, "start") as start, \
                mock.patch.object(session_identity, "known_sub_session_root", return_value=SID):
            self.assertIsNone(redirect.redirect("claude", {**claude(subagent_type="light"), "cwd": str(self.project)}))
        start.assert_not_called()


class WhoIsLeftAloneTest(Base):
    def test_a_registered_worker_or_a_native_subagent_starts_nothing(self):
        with mock.patch.object(redirect, "start") as start:
            for env in ({"AGENT_SESSION_ROLE": "worker"}, {"AGENT_DISPATCH_CHILD": "1"},
                        {"AGENT_SESSION_ROLE": "owner"}, {"AGENT_DISPATCH_DEPTH": "1"}):
                with self.subTest(env=env), mock.patch.dict(os.environ, env):
                    self.assertIsNone(redirect.redirect("claude", claude(subagent_type="light")))
            self.assertIsNone(redirect.redirect("claude", {**claude(subagent_type="light"), "agent_id": "a-1"}))
            for root in ("root-session", None):
                with mock.patch.object(session_identity, "known_sub_session_root", return_value=root):
                    self.assertIsNone(redirect.redirect("codex", codex(agent_type="light")))
        start.assert_not_called()

    def test_a_codex_thread_the_state_db_cannot_vouch_for_is_unknown(self):
        with tempfile.TemporaryDirectory() as home:
            env = {"CODEX_HOME": home}
            self.assertIsNone(session_identity.known_sub_session_root("codex", "thread-1", env))   # no DB
            self.assertEqual(session_identity.sub_session_root("codex", "thread-1", env), "thread-1")
            connection = sqlite3.connect(os.path.join(home, "state_5.sqlite"))
            connection.execute("CREATE TABLE thread_spawn_edges (parent_thread_id TEXT, child_thread_id TEXT)")
            connection.execute("INSERT INTO thread_spawn_edges VALUES ('thread-root', 'thread-child')")
            connection.commit()
            self.assertIsNone(session_identity.known_sub_session_root("codex", "thread-1", env))   # no threads table
            connection.execute("CREATE TABLE threads (id TEXT PRIMARY KEY)")
            connection.commit()
            self.assertIsNone(session_identity.known_sub_session_root("codex", "thread-1", env))   # no row for it
            self.assertEqual(session_identity.sub_session_root("codex", "thread-1", env), "thread-1")
            connection.executemany("INSERT INTO threads VALUES (?)", [("thread-1",), ("thread-child",)])
            connection.commit()
            connection.close()
            self.assertEqual(session_identity.known_sub_session_root("codex", "thread-1", env), "thread-1")
            self.assertEqual(session_identity.known_sub_session_root("codex", "thread-child", env), "thread-root")


class StartTest(Base):
    """Hearting holds the call once its start receipt says so; a compose that registers nothing leaves it native.
    The hook never stops a start."""

    def setUp(self):
        super().setUp()
        self.dir = Path(tempfile.mkdtemp(prefix="nsr-"))
        self.addCleanup(shutil.rmtree, self.dir, True)
        (self.dir / "capability-route.py").write_text(FAKE_ROUTE)
        self.record = self.dir / "record.jsonl"
        for patcher in (mock.patch.object(redirect, "UTILITIES", self.dir),
                        mock.patch.dict(os.environ, {"FAKE_RECORD": str(self.record)})):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(self.end)

    def pid(self):
        path = Path(str(self.record) + ".pid")
        return int(path.read_text()) if path.is_file() else None

    def end(self):
        if self.pid():
            try:
                os.killpg(self.pid(), signal.SIGKILL)   # the fake compose ran in its own group
            except OSError:
                pass

    def records(self):
        return [json.loads(line) for line in self.record.read_text().splitlines()] if self.record.is_file() else []

    def start(self, mode, call=None, harness="claude"):
        call = call or redirect.normalized("claude", claude(subagent_type="Explore"))
        with mock.patch.dict(os.environ, {"FAKE_MODE": mode, "CODEX_THREAD_ID": "inherited-thread",
                                          "AGENT_DISPATCH_CALLER_HARNESS": "codex"}):
            return redirect.start(harness, call, "light", os.getcwd())

    def test_a_running_receipt_and_what_the_compose_process_got(self):
        receipt = self.start("running")
        self.assertEqual((receipt["route_id"], receipt["state"]), ("rt-0123456789abcdef", "running"))
        [record] = self.records()
        self.assertEqual(record["argv"], ["compose", "--start", "--unit", "general/explore", "--profile", "light",
                                          "--ephemeral", "--prompt-file", "-", "--cwd", os.getcwd()])
        self.assertEqual(record["brief"], "Find the panel rule\n\nWhere is the panel rule?\n")
        # The session's own identity, as its tool commands carry it, and no caller waiting on the start.
        self.assertEqual(record["env"], {"CLAUDE_CODE_SESSION_ID": SID, "CODEX_THREAD_ID": None,
                                         "OPENCODE_SESSION_ID": None, "AGENT_DISPATCH_CALLER_HARNESS": None,
                                         "AGENT_CAPACITY_RESUME_CHAIN": "0"})
        call = redirect.normalized("codex", codex(agent_type="explorer"))
        self.start("running", call=call, harness="codex")
        env = self.records()[1]["env"]
        self.assertEqual((env["CODEX_THREAD_ID"], env["CLAUDE_CODE_SESSION_ID"]), (SID, None))

    def test_a_full_worker_slot_waits_in_hearting_s_queue(self):
        receipt = self.start("capacity")
        self.assertEqual(receipt["state"], "waiting-capacity")
        line = redirect.reason_for(receipt, "light")
        self.assertTrue(line.startswith("Queued as Hearting unit route rt-0123456789abcdef (general/explore, light, "
                                        "no record). It waits for a free worker and does not run natively."), line)
        self.assertIn("comes back to this session as a completion notice", line)
        self.assertNotIn("idle", line)
        self.assertIn("If this session went idle before the queued worker started, the notice comes with the next message.",
                      redirect.reason_for(receipt, "light", "claude"))

    def test_the_first_capacity_refusal_of_this_start_arms_the_runtime_s_resume(self):
        # The receipt `_capacity_wait` gives the first refusal; with this start's environment the existing capacity
        # resume takes the wait (a later start, then the notice) instead of handing the session a wait command.
        import capacity_auto_resume
        refusal = {"state": "waiting-capacity", "reason": "launch-capacity-wait", "route_id": "rt-1",
                   "required_action": "resume-after-capacity", "retry_at": "2099-01-01T00:00:00Z"}
        spawn = mock.Mock()
        env = redirect.compose_env("claude", SID)
        armed = capacity_auto_resume.arm(refusal, self.dir / "rt-1.json", self.dir / "jobs.log", environ=env,
                                         spawn=spawn, now=lambda: 4070908800 - 60)
        self.assertEqual((armed["state"], armed["cause"]), ("armed", "launch-capacity"))
        spawn.assert_called_once()
        self.assertIsNone(capacity_auto_resume.arm(refusal, self.dir / "rt-1.json", self.dir / "jobs.log",
                                                   environ={}, spawn=spawn))   # a typing session waits itself

    def test_a_compose_that_registers_nothing_leaves_the_native_call(self):
        for mode in ("refused", "garbage", "not-admitted"):
            with self.subTest(mode=mode):
                self.assertIsNone(self.start(mode))

    def test_once_registered_a_later_failure_still_refuses_the_native_call(self):
        # The receipt said Hearting holds it; what follows (the compose failing, an attempt that ended or a row
        # claimed before the start raised) reaches the session through the runtime's notice, never natively.
        for mode in ("running-then-fails", "registered-then-raised", "ended"):
            with self.subTest(mode=mode):
                receipt = self.start(mode)
                self.assertTrue(redirect.registered(receipt), mode)
        line = redirect.reason_for(self.start("registered-then-raised"), "light")
        self.assertIn("Its answer, or what stopped it, comes back to this session as a completion notice", line)
        line = redirect.reason_for(self.start("ended"), "light")
        self.assertIn("It has already ended; read its result once with: cr start --route r", line)
        self.assertNotIn("cr compose", line)

    def test_the_hook_returns_at_the_receipt_and_never_stops_the_start(self):
        began = time.monotonic()
        receipt = self.start("linger")
        self.assertLess(time.monotonic() - began, 10)
        self.assertEqual(receipt["state"], "running")
        self.assertTrue(alive(self.pid()))                 # its bookkeeping goes on, alone

    def test_a_worker_the_start_launched_does_not_hold_the_hook(self):
        began = time.monotonic()
        self.assertIsNotNone(self.start("late-grandchild"))
        self.assertLess(time.monotonic() - began, 15)
        self.assertTrue(gone(int(Path(str(self.record) + ".late").read_text()), 15))

    def test_a_large_brief_never_waits_on_a_start_that_does_not_read_it(self):
        call = redirect.normalized("claude", claude(subagent_type="Explore", prompt="x" * 400_000))
        began = time.monotonic()
        self.assertEqual(self.start("no-read", call=call)["route_id"], "rt-0123456789abcdef")
        self.assertLess(time.monotonic() - began, 10)

    def test_a_missing_cwd_or_a_broken_payload_runs_natively(self):
        self.assertIsNone(redirect.redirect("claude", {**claude(subagent_type="light"), "cwd": "/nonexistent/x"}))
        self.assertIsNone(redirect.redirect("claude", "not a dict"))
        self.assertIsNone(redirect.redirect("unknown", claude(subagent_type="light")))
        self.assertEqual(self.records(), [])


class ReasonTest(Base):
    """The line follows the start receipt's own way back."""

    def test_end_turn_promises_the_notice(self):
        line = redirect.reason_for({"route_id": "rt-1", "state": "running", "parent_next": "end-turn"}, "light")
        self.assertEqual(line, "Started as Hearting unit route rt-1 (general/explore, light, no record). Its answer, "
                               "or what stopped it, comes back to this session as a completion notice. Do not call it "
                               "again; go on with other work or end the turn.")

    def test_bounded_wait_gives_the_wait_command_and_promises_no_notice(self):
        for state in ("running", "waiting-capacity"):
            line = redirect.reason_for({"route_id": "rt-1", "state": state, "parent_next": "bounded-wait",
                                        "parent_next_command": "python3 cr.py start --route r --wait"}, "light")
            self.assertIn("No completion notice reaches this session", line)
            self.assertIn("python3 cr.py start --route r --wait", line)
            self.assertNotIn("as a completion notice", line)

    def test_a_finished_receipt_carries_its_answer(self):
        line = redirect.reason_for({"route_id": "rt-1", "state": "completed", "summary": "Answered: x"}, "light")
        self.assertIn("It has already finished; its answer:\nAnswered: x", line)
        line = redirect.reason_for({"route_id": "rt-1", "state": "completed", "resume_command": "resume"}, "light")
        self.assertIn("read its result once with: resume", line)

    def test_an_ended_receipt_with_no_command_promises_the_notice(self):
        # A unit cancelled right after its start: `work_start` leaves `resume_command` empty.
        line = redirect.reason_for({"route_id": "rt-1", "state": "cancelled", "result": {"stopped": True},
                                    "resume_command": "", "parent_next": "inspect-receipt"}, "light")
        self.assertIn("Its answer, or what stopped it, comes back to this session as a completion notice.", line)
        self.assertNotIn("read its result once with", line)


class EntryTest(Base):
    def test_main_prints_the_deny_json_and_never_fails(self):
        line = "Started as Hearting unit route rt-1"
        with mock.patch.object(redirect, "redirect", return_value=line):
            for flag in ("--claude", "--codex"):
                out = io.StringIO()
                with mock.patch.object(sys, "stdout", out):
                    self.assertEqual(redirect.main([flag], io.StringIO(json.dumps(claude()))), 0)
                decision = json.loads(out.getvalue())["hookSpecificOutput"]
                self.assertEqual((decision["permissionDecision"], decision["permissionDecisionReason"]),
                                 ("deny", line))
        with mock.patch.object(redirect, "redirect", return_value=None):
            out = io.StringIO()
            with mock.patch.object(sys, "stdout", out):
                self.assertEqual(redirect.main(["--codex"], io.StringIO("{}")), 0)
            self.assertEqual(out.getvalue(), "")
        with mock.patch.object(redirect, "redirect", side_effect=RuntimeError("boom")):
            self.assertEqual(redirect.main(["--claude"], io.StringIO("{}")), 0)
        self.assertEqual(redirect.main(["--claude"], io.StringIO("{not json")), 0)

    def test_the_registrations_this_hook_counts_as_its_own(self):
        claude_group, codex_group = ours("claude"), ours("codex")
        self.assertTrue(claude_group["hooks"][0]["command"].endswith("subagent-model-default.sh --redirect"))
        self.assertIn("pretooluse-subagent-redirect.py", codex_group["hooks"][0]["command"])
        self.assertEqual([len(claude_group["hooks"]), len(codex_group["hooks"])], [1, 1])
        bridge = ROOT / "adapters/codex/hooks/pretooluse-subagent-redirect.py"
        self.assertTrue(os.access(bridge, os.X_OK))
        self.assertIn("pretooluse-subagent-redirect.py", (ROOT / "adapters/codex/hooks/run-hook.sh").read_text())
        done = subprocess.run([sys.executable, str(bridge)], input=json.dumps(codex(agent_type="light")), text=True,
                              capture_output=True, timeout=60,
                              env={"PATH": "/usr/bin:/bin", "HOME": os.environ.get("HOME", "/tmp"),
                                   "PYTHONDONTWRITEBYTECODE": "1", "AGENT_SESSION_ROLE": "worker"})
        self.assertEqual((done.returncode, done.stdout), (0, ""))
        plugin = (ROOT / "adapters/opencode/plugins/hearting-guards.js").read_text()
        self.assertNotIn("native_subagent_redirect", plugin)   # OpenCode leaves its subagent calls native


class CodexRunnerTest(unittest.TestCase):
    """`adapters/codex/hooks/run-hook.sh`: no failure of the redirect bridge reaches Codex as exit 2."""

    def run_hook(self, script, bridge_text=None):
        root = Path(tempfile.mkdtemp(prefix="nsr-codex-"))
        self.addCleanup(shutil.rmtree, root, True)
        (root / "core").mkdir()
        (root / "core/CORE.md").write_text("fixture\n")
        hooks = root / "adapters/codex/hooks"
        hooks.mkdir(parents=True)
        shutil.copy(ROOT / "adapters/codex/hooks/run-hook.sh", hooks / "run-hook.sh")
        if bridge_text is not None:
            (hooks / script).write_text(bridge_text)
            (hooks / script).chmod(0o755)
        return subprocess.run(["sh", str(hooks / "run-hook.sh"), script], input="{}", text=True,
                              capture_output=True, timeout=60,
                              env={"PATH": "/usr/bin:/bin", "HOME": str(root), "AGENT_HOME": str(root),
                                   "PYTHONDONTWRITEBYTECODE": "1"})

    def test_a_partial_install_without_the_bridge_lets_the_call_run(self):
        done = self.run_hook("pretooluse-subagent-redirect.py")
        self.assertEqual((done.returncode, done.stdout, done.stderr), (0, "", ""))

    def test_a_bridge_that_fails_or_whose_file_went_away_lets_the_call_run(self):
        # What python prints when the file it was handed is gone by the time it opens it, and a crash.
        for text in ("import sys\nsys.stderr.write(\"can't open file: No such file\\n\")\nraise SystemExit(2)\n",
                     "raise RuntimeError('boom')\n"):
            with self.subTest(text=text):
                done = self.run_hook("pretooluse-subagent-redirect.py", text)
                self.assertEqual((done.returncode, done.stdout, done.stderr), (0, "", ""))

    def test_a_started_route_passes_its_deny_json(self):
        deny = '{"hookSpecificOutput":{"permissionDecision":"deny"}}'
        done = self.run_hook("pretooluse-subagent-redirect.py", f"print({deny!r})\n")
        self.assertEqual((done.returncode, done.stdout), (0, deny))

    def test_other_bridges_keep_their_exit_two(self):
        done = self.run_hook("core-write-guard.py", "import sys\nsys.stderr.write('refused\\n')\nraise SystemExit(2)\n")
        self.assertEqual((done.returncode, done.stderr), (2, "refused\n"))


class ClaudeHookTest(Base):
    """`hooks/subagent-model-default.sh`: the model policy decides first; with `--redirect` a call it lets
    through may then be redirected; without it (an older session's line) nothing is redirected."""

    def run_hook(self, args, payload, reason):
        tree = Path(tempfile.mkdtemp(prefix="nsr-hook-"))
        self.addCleanup(shutil.rmtree, tree, True)
        (tree / "hooks").mkdir()
        (tree / "utilities").mkdir()
        shutil.copy(ROOT / "hooks/subagent-model-default.sh", tree / "hooks/subagent-model-default.sh")
        (tree / "config").mkdir()
        (tree / "config/models.conf").write_text("CFG_TIER_LIGHT_MODEL=sonnet\nCFG_NATIVE_SUBAGENT=light\n"
                                                 "CFG_MAIN_SESSION_ONLY_MODELS=\"fable\"\n")
        self.called = tree / "called"
        (tree / "utilities/native_subagent_redirect.py").write_text(
            "def redirect(harness, payload):\n"
            f"    open({str(self.called)!r}, 'w').write('1')\n"
            f"    return {reason!r}\n")
        done = subprocess.run(["bash", str(tree / "hooks/subagent-model-default.sh"), *args],
                              input=json.dumps(payload), text=True, capture_output=True, timeout=60,
                              env={"PATH": "/usr/bin:/bin", "HOME": str(tree), "PYTHONDONTWRITEBYTECODE": "1"})
        self.assertEqual(done.returncode, 0, done.stderr)
        return json.loads(done.stdout)["hookSpecificOutput"] if done.stdout.strip() else {}

    def test_a_started_route_is_refused_with_its_line(self):
        out = self.run_hook(["--redirect"], claude(subagent_type="Explore"), "Started as Hearting unit route rt-1")
        self.assertEqual((out["permissionDecision"], out["permissionDecisionReason"]),
                         ("deny", "Started as Hearting unit route rt-1"))

    def test_a_call_that_was_not_taken_gets_the_model_as_before(self):
        out = self.run_hook(["--redirect"], claude(subagent_type="Explore"), None)
        self.assertEqual(out["updatedInput"]["model"], "sonnet")
        self.assertNotIn("permissionDecision", out)

    def test_the_old_registration_line_redirects_nothing(self):
        out = self.run_hook([], claude(subagent_type="Explore"), "Started as Hearting unit route rt-1")
        self.assertEqual(out["updatedInput"]["model"], "sonnet")
        self.assertFalse(self.called.exists())

    def test_a_call_the_model_policy_refuses_is_refused_and_starts_nothing(self):
        for payload, reason in ((claude(subagent_type="fork"), "native-subagent-model-inheritance-ineligible"),
                                (claude(subagent_type="Explore", model="inherit"),
                                 "native-subagent-model-inheritance-ineligible"),
                                (claude(subagent_type="Explore", model="fable"),
                                 "native-subagent-main-session-only-model")):
            with self.subTest(payload=payload["tool_input"]):
                out = self.run_hook(["--redirect"], payload, "Started as Hearting unit route rt-1")
                self.assertEqual(out["permissionDecisionReason"], reason)
                self.assertFalse(self.called.exists())       # the redirect never ran: nothing started


if __name__ == "__main__":
    unittest.main()
