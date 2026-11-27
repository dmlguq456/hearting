#!/usr/bin/env python3
"""A person's session's native subagent call starts as a Hearting unit route only when that is certain."""
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
import textwrap
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
import native_subagent_redirect as redirect  # noqa: E402
import session_identity  # noqa: E402
import work_start  # noqa: E402,F401 -- the real module, before a fixture tree puts its own on the path

SID = "native-subagent-session"
WORKER_ENV = ("AGENT_SESSION_ROLE", "AGENT_DISPATCH_CHILD", "AGENT_DISPATCH_DEPTH", "FLEET_TITLE_REFRESH", "MEM_DISTILL")


def claude(**args):
    return {"session_id": SID, "hook_event_name": "PreToolUse", "tool_name": "Agent", "cwd": os.getcwd(),
            "tool_input": {"description": "Find the panel rule", "prompt": "Where is the panel rule?", **args}}


def codex(tool="spawn_agent", **args):
    return {"session_id": SID, "hook_event_name": "PreToolUse", "tool_name": tool, "cwd": os.getcwd(),
            "tool_input": {"message": "Where is the panel rule?", **args}}


def gone(pid, seconds=10.0):
    """True once `pid` no longer exists (reaping a child of this process too)."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            if os.waitpid(pid, os.WNOHANG)[0] == pid:
                return True
        except ChildProcessError:
            pass
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        try:   # a zombie no one has reaped yet has ended too
            if Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] == "Z":
                return True
        except (OSError, IndexError):
            pass
        time.sleep(0.05)
    return False


def end_group(pid):
    try:
        os.killpg(pid, signal.SIGKILL)
    except OSError:
        pass


# A stand-in for `capability-route.py`: it records what the hook gave it (argv, the brief, the internal value,
# the session identity) and answers the way FAKE_MODE names.
FAKE_ROUTE = r'''
import json, os, sys, time, subprocess
import work_start
mode = os.environ.get("FAKE_MODE", "running")
base = os.environ["FAKE_RECORD"]
open(base + ".pid", "w").write(str(os.getpid()))
if mode == "slow-read":
    time.sleep(3)                                   # the start is slow to take its input
brief = "" if mode == "no-read" else sys.stdin.read()
record = {"argv": sys.argv[1:], "brief": brief, "reads_refusal": work_start.CALLER_READS_REFUSAL,
          "path0": sys.path[0], "cwd_on_path": "" in sys.path or "." in sys.path,
          "env": {k: os.environ.get(k) for k in ("CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID", "OPENCODE_SESSION_ID",
                                                  "AGENT_DISPATCH_CALLER_HARNESS")}}
with open(base, "a") as fh:
    fh.write(json.dumps(record) + "\n")

def route(route_id):
    path = base + ".route.json"
    open(path, "w").write(json.dumps({"route_id": route_id, "artifact_root": os.path.dirname(base)}))
    print("route_file=" + path, file=sys.stderr, flush=True)
    return path

if mode in ("running", "no-read", "slow-read"):
    print("compose_decision fixture", file=sys.stderr)
    print(json.dumps({"state": "running", "route_id": "rt-0123456789abcdef", "unit_attempt_id": "att-x",
                      "parent_next": "end-turn"}))
elif mode == "linger":
    # The receipt is flushed; the bookkeeping after it is slow (or stalls past the budget).
    print(json.dumps({"state": "running", "route_id": "rt-linger", "parent_next": "end-turn"}), flush=True)
    time.sleep(30)
elif mode == "late-grandchild":
    # A worker the start launched keeps running (and the output files open) after the start returns.
    late = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(3)"], start_new_session=True)
    open(base + ".late", "w").write(str(late.pid))
    print(json.dumps({"state": "running", "route_id": "rt-0123456789abcdef", "parent_next": "end-turn"}))
elif mode == "capacity":
    route("rt-1")
    print(json.dumps({"state": "waiting-capacity", "reason": "launch-capacity-wait", "route_id": "rt-1"}))
elif mode == "registered":
    path = route("rt-registered")
    print(json.dumps({"state": "needs-attention", "reason": "OSError", "route_id": "rt-registered",
                      "route_file": path, "registered_attempts": ["att-r"]}))
elif mode == "route-then-hang":
    route("rt-hang")
    time.sleep(60)
elif mode == "refused":
    print("capability-route: quick-headless-unavailable", file=sys.stderr)
    raise SystemExit(64)
elif mode == "garbage":
    print("not json")
elif mode == "hang":
    time.sleep(60)
'''


class FakeTree:
    """A utilities folder whose `capability-route.py` is FAKE_ROUTE and whose `work_start` carries the value."""

    def __init__(self, test):
        self.dir = Path(tempfile.mkdtemp(prefix="nsr-"))
        test.addCleanup(shutil.rmtree, self.dir, True)
        (self.dir / "capability-route.py").write_text(FAKE_ROUTE)
        (self.dir / "work_start.py").write_text("CALLER_READS_REFUSAL = False\n")
        self.record = self.dir / "record.jsonl"
        test.addCleanup(mock.patch.stopall)
        mock.patch.object(redirect, "UTILITIES", self.dir).start()
        test.addCleanup(self.end)

    def path(self, suffix):
        return Path(str(self.record) + suffix)

    def pid(self):
        return int(self.path(".pid").read_text()) if self.path(".pid").is_file() else None

    def end(self):
        if self.pid():
            end_group(self.pid())

    def records(self):
        return [json.loads(line) for line in self.record.read_text().splitlines()] if self.record.is_file() else []


class Base(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ, {key: "" for key in WORKER_ENV})
        patcher.start()
        self.addCleanup(patcher.stop)
        for key in WORKER_ENV:
            os.environ.pop(key, None)


class Homes(Base):
    """Claude and Codex homes and a project folder with nothing in them but what a test puts there."""

    def setUp(self):
        super().setUp()
        self.tmp = Path(tempfile.mkdtemp(prefix="nsr-home-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.claude_home, self.codex_home = self.tmp / "claude", self.tmp / "codex"
        self.project = self.tmp / "project" / "sub"
        for folder in (self.claude_home, self.codex_home, self.project):
            folder.mkdir(parents=True)
        patcher = mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.claude_home),
                                               "CODEX_HOME": str(self.codex_home), "CLAUDE_PROJECT_DIR": ""})
        patcher.start()
        self.addCleanup(patcher.stop)

    def write(self, path, text):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def ship(self, harness, name):
        suffix = ".md" if harness == "claude" else ".toml"
        home = self.claude_home if harness == "claude" else self.codex_home
        return self.write(home / "agents" / (name + suffix),
                          (ROOT / "adapters" / harness / "agents" / (name + suffix)).read_text())


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

    def test_no_name(self):
        self.assertEqual(self.depth("claude", claude()), "balanced-deep")
        self.assertEqual(self.depth("codex", codex()), "balanced")
        with self.assertRaises(redirect.Skip):
            self.depth("claude", claude(subagent_type="general"))

    def test_a_named_model_or_effort_picks_the_depth_it_is_declared_for(self):
        profiles = {"light": {"model": "luna", "effort": "medium"}, "balanced": {"model": "luna", "effort": "high"},
                    "balanced-deep": {"model": "sol", "effort": "medium"}, "deep": {"model": "sol", "effort": "xhigh"}}
        with mock.patch.object(redirect, "_profiles", return_value=profiles):
            self.assertEqual(self.depth("codex", codex(agent_type="deep", model="sol")), "deep")
            self.assertEqual(self.depth("codex", codex(agent_type="explorer", model="sol")), "balanced-deep")
            self.assertEqual(self.depth("codex", codex(agent_type="light", reasoning_effort="high")), "balanced")
            self.assertEqual(self.depth("codex", codex(agent_type="deep", model="sol", reasoning_effort="medium")),
                             "balanced-deep")
            self.assertEqual(self.depth("claude", claude(subagent_type="deep", model="luna")), "light")
            for payload in (codex(agent_type="light", model="haiku"), codex(agent_type="light", reasoning_effort="low"),
                            codex(agent_type="light", model="luna", reasoning_effort="xhigh")):
                with self.subTest(payload=payload["tool_input"]), self.assertRaises(redirect.Skip):
                    self.depth("codex", payload)       # nothing declares it: the native call keeps it

    def test_the_shipped_models_name_the_depths(self):
        self.assertEqual({d: p["model"] for d, p in redirect._profiles("claude").items()},
                         {"light": "sonnet", "balanced": "sonnet", "balanced-deep": "opus", "deep": "opus"})
        self.assertEqual({d: p["effort"] for d, p in redirect._profiles("codex").items()},
                         {"light": "medium", "balanced": "high", "balanced-deep": "medium", "deep": "xhigh"})

    def test_forms_the_brief_cannot_carry_are_left_alone(self):
        for harness, payload in (
                ("claude", claude(subagent_type="fork")),
                ("claude", claude(subagent_type="web-fetch")),
                ("claude", claude(subagent_type="statusline-setup")),
                ("claude", claude(subagent_type="claude-code-guide")),
                ("claude", claude(subagent_type="claude")),
                ("claude", claude(subagent_type="my-plugin:reviewer")),
                ("claude", claude(subagent_type="light", isolation="worktree")),
                ("claude", claude(subagent_type="light", resume="agent-1")),
                ("claude", claude(subagent_type="light", team_name="t")),       # an argument the table does not know
                ("claude", claude(subagent_type="light", prompt="")),
                ("codex", codex(agent_type="light", fork_context=True)),
                ("codex", codex("collaborationspawn_agent", agent_type="light", task_name="t")),
                ("codex", codex("collaborationspawn_agent", agent_type="light", task_name="t", fork_turns="all")),
                ("codex", codex(agent_type="light", task_name="t")),            # v2 by its arguments, all turns
                ("codex", codex("collaborationspawn_agent", agent_type="light", task_name="t", fork_turns="none",
                                fork_context=False)),                          # v2 refuses fork_context
                ("codex", {**codex(agent_type="light"), "tool_input": {"items": [{"type": "text", "text": "x"}]}}),
                ("codex", codex(agent_type="light", items=[{"type": "text", "text": "x"}])),   # message and items
                ("claude", {**claude(subagent_type="light"), "session_id": ""}),
                ("claude", {**claude(subagent_type="light"), "tool_name": "Bash"})):
            with self.subTest(harness=harness, payload=payload):
                with self.assertRaises(redirect.Skip):
                    self.depth(harness, payload)

    def test_codex_v2_starts_only_when_no_history_is_handed_over(self):
        for tool in ("collaborationspawn_agent", "spawn_agent"):
            payload = codex(tool, agent_type="explorer", task_name="look", fork_turns=" none ")
            call = redirect.normalized("codex", payload)
            self.assertEqual((call["title"], redirect.depth_for("codex", call)), ("look", "light"))

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
    """The definition the harness picks for the name, not the name alone (review 🔴5)."""

    def check(self, harness, name):
        try:
            redirect.require_shipped_definition(harness, name, str(self.project))
            return True
        except redirect.Skip:
            return False

    def test_claude_hearting_definitions_shipped_linked_or_rendered(self):
        self.ship("claude", "light")
        self.assertTrue(self.check("claude", "light"))
        (self.claude_home / "agents" / "deep.md").symlink_to(ROOT / "adapters/claude/agents/deep.md")
        self.assertTrue(self.check("claude", "deep"))
        # Activation renders the user's own model pin into the file; the rest is Hearting's text.
        shipped = (ROOT / "adapters/claude/agents/balanced.md").read_text()
        self.write(self.claude_home / "agents/balanced.md",
                   shipped.replace("\nmodel: sonnet\n", "\nmodel: haiku\n").replace("\neffort: high\n", "\neffort: low\n"))
        self.assertTrue(self.check("claude", "balanced"))
        self.assertFalse(self.check("claude", "memory-scout"))             # no definition at all: not certain

    def test_claude_built_ins_unless_some_scope_defines_the_name(self):
        for name in ("Explore", "Plan", "general-purpose"):
            self.assertTrue(self.check("claude", name), name)
        self.write(self.project.parent / ".claude/agents/scouts/mine.md", "---\nname: Explore\n---\nMine.\n")
        self.assertFalse(self.check("claude", "Explore"))                 # a project agent walking up wins
        self.write(self.claude_home / "agents/plan.md", "---\nname: Plan\ndescription: mine\n---\nMine.\n")
        self.assertFalse(self.check("claude", "Plan"))                    # the user's own

    def test_claude_a_light_name_collision_keeps_the_native_call(self):
        self.ship("claude", "light")
        self.write(self.project / ".claude/agents/reviewer.md",
                   "---\nname: light\ndescription: The project's own\ntools: Read\n---\nProject prompt.\n")
        self.assertFalse(self.check("claude", "light"))
        (self.project / ".claude/agents/reviewer.md").unlink()
        self.write(self.claude_home / "agents/light.md",
                   "---\nname: light\ndescription: Mine\n---\nMy own light agent.\n")
        self.assertFalse(self.check("claude", "light"))

    def test_claude_project_dir_and_unreadable_definitions(self):
        self.ship("claude", "light")
        other = self.tmp / "elsewhere"
        self.write(other / ".claude/agents/x.md", "---\nname: light\n---\nx\n")
        with mock.patch.dict(os.environ, {"CLAUDE_PROJECT_DIR": str(other)}):
            self.assertFalse(self.check("claude", "light"))
        (self.claude_home / "agents/bad.md").write_bytes(b"---\nname: \xff\n---\n")
        self.assertFalse(self.check("claude", "light"))

    def test_codex_roles_by_their_name_field(self):
        self.ship("codex", "light")
        self.assertTrue(self.check("codex", "light"))
        for name in ("default", "worker", "explorer"):
            self.assertTrue(self.check("codex", name), name)
        self.write(self.codex_home / "agents/scout.toml",
                   'name = "explorer"\ndescription = "mine"\ndeveloper_instructions = "Mine."\n')
        self.assertFalse(self.check("codex", "explorer"))                 # a user role overrides the built-in
        self.write(self.project / ".codex/agents/x.toml",
                   'name = "light"\ndescription = "p"\ndeveloper_instructions = "P."\n')
        self.assertFalse(self.check("codex", "light"))                    # a project layer comes first
        self.write(self.codex_home / "config.toml", '[agents.worker]\ndescription = "mine"\n')
        self.assertFalse(self.check("codex", "worker"))                   # a declared role
        self.assertTrue(self.check("codex", "default"))

    def test_the_redirect_starts_nothing_for_a_collision(self):
        self.ship("claude", "light")
        self.write(self.project / ".claude/agents/light.md", "---\nname: light\n---\nMine.\n")
        with mock.patch.object(redirect, "start") as start, \
                mock.patch.object(session_identity, "known_sub_session_root", return_value=SID):
            self.assertIsNone(redirect.redirect("claude", {**claude(subagent_type="light"), "cwd": str(self.project)}))
        start.assert_not_called()


class OtherDecisionTest(Homes):
    """Another hook or a permission rule on the same call (review 🔴4)."""

    OURS_CLAUDE = {"matcher": "Agent|Task", "hooks": [{"type": "command", "timeout": 180,
                   "command": '"${CLAUDE_CONFIG_DIR:-$HOME/.claude}/hooks/run-hook.sh" bash subagent-model-default.sh --redirect'}]}
    OURS_CODEX = {"matcher": "spawn_agent|collaborationspawn_agent", "hooks": [{"type": "command", "timeout": 180,
                  "command": "sh -c 'exec run-hook.sh pretooluse-subagent-redirect.py'"}]}

    def sole(self, harness, tool):
        try:
            redirect.require_sole_decision(harness, tool, str(self.project))
            return True
        except redirect.Skip:
            return False

    def settings(self, path, *groups, **extra):
        return self.write(path, json.dumps({"hooks": {"PreToolUse": list(groups)}, **extra}))

    def test_claude_only_this_hook(self):
        self.settings(self.claude_home / "settings.json", self.OURS_CLAUDE,
                      {"matcher": "Bash", "hooks": [{"type": "command", "command": "guard"}]},
                      permissions={"allow": ["Agent"]})
        self.assertTrue(self.sole("claude", "Agent"))

    def test_claude_another_matching_hook_or_rule(self):
        refuse = {"type": "command", "command": "my-refusal"}
        cases = [
            (self.claude_home / "settings.json", [self.OURS_CLAUDE, {"matcher": "Agent", "hooks": [refuse]}], {}),
            (self.claude_home / "settings.json", [self.OURS_CLAUDE, {"matcher": "*", "hooks": [refuse]}], {}),
            (self.claude_home / "settings.json", [self.OURS_CLAUDE, {"hooks": [refuse]}], {}),
            (self.claude_home / "settings.json", [self.OURS_CLAUDE, {"matcher": "^Ag", "hooks": [refuse]}], {}),
            (self.claude_home / "settings.json", [self.OURS_CLAUDE, {"matcher": "Task", "hooks": [refuse]}], {}),
            (self.claude_home / "settings.json", [self.OURS_CLAUDE],
             {"permissions": {"deny": ["Agent(Explore)"]}}),
            (self.claude_home / "settings.json", [self.OURS_CLAUDE], {"permissions": {"ask": ["Task"]}}),
            (self.project.parent / ".claude/settings.local.json", [{"matcher": "Agent", "hooks": [refuse]}], {}),
            (self.project / ".claude/settings.json", [], {"permissions": {"deny": ["Agent"]}}),
        ]
        for path, groups, extra in cases:
            with self.subTest(path=str(path), groups=groups, extra=extra):
                self.settings(self.claude_home / "settings.json", self.OURS_CLAUDE)
                self.settings(path, *groups, **extra)
                self.assertFalse(self.sole("claude", "Agent"))
                for leftover in (self.project.parent / ".claude", self.project / ".claude"):
                    shutil.rmtree(leftover, ignore_errors=True)

    def test_claude_plugin_hooks_and_unreadable_settings(self):
        plugin = self.claude_home / "plugins/cache/market/tool/1.0.0"
        self.write(plugin / "hooks/hooks.json", json.dumps({"hooks": {"Stop": [{"hooks": []}]}}))
        self.assertTrue(self.sole("claude", "Agent"))
        self.write(plugin / ".claude-plugin/plugin.json", json.dumps(
            {"name": "tool", "hooks": {"PreToolUse": [{"matcher": "", "hooks": [{"type": "command", "command": "x"}]}]}}))
        self.assertFalse(self.sole("claude", "Agent"))
        (plugin / ".claude-plugin/plugin.json").unlink()
        self.assertTrue(self.sole("claude", "Agent"))
        self.write(self.claude_home / "settings.json", "{not json")
        self.assertFalse(self.sole("claude", "Agent"))

    def test_codex_layers_inline_hooks_and_plugins(self):
        self.write(self.codex_home / "hooks.json", json.dumps({"hooks": {"PreToolUse": [self.OURS_CODEX]}}))
        self.assertTrue(self.sole("codex", "spawn_agent"))
        refuse = {"matcher": "spawn_agent", "hooks": [{"type": "command", "command": "my-refusal"}]}
        self.write(self.project / ".codex/hooks.json", json.dumps({"hooks": {"PreToolUse": [refuse]}}))
        self.assertFalse(self.sole("codex", "spawn_agent"))
        shutil.rmtree(self.project / ".codex")
        self.write(self.codex_home / "config.toml",
                   '[[hooks.PreToolUse]]\nmatcher = ".*agent"\n[[hooks.PreToolUse.hooks]]\ntype = "command"\n'
                   'command = "my-refusal"\n')
        self.assertFalse(self.sole("codex", "spawn_agent"))
        self.write(self.codex_home / "config.toml",
                   '[[hooks.PreToolUse]]\nmatcher = "shell"\n[[hooks.PreToolUse.hooks]]\ntype = "command"\n'
                   'command = "guard"\n')
        self.assertTrue(self.sole("codex", "spawn_agent"))
        self.write(self.codex_home / "plugins/cache/m/p/1/hooks/hooks.json",
                   json.dumps({"hooks": {"PreToolUse": [{"matcher": "*", "hooks": [{"command": "x"}]}]}}))
        self.assertFalse(self.sole("codex", "spawn_agent"))

    def test_a_call_another_hook_refuses_starts_nothing(self):
        # Another hook refusing the same call: no compose runs, so no row is registered and no model is asked.
        self.ship("claude", "light")
        self.settings(self.claude_home / "settings.json", self.OURS_CLAUDE,
                      {"matcher": "Agent", "hooks": [{"type": "command", "command": "exit 2"}]})
        tree = FakeTree(self)
        with mock.patch.object(session_identity, "known_sub_session_root", return_value=SID), \
                mock.patch.dict(os.environ, {"FAKE_RECORD": str(tree.record)}):
            self.assertIsNone(redirect.redirect("claude", {**claude(subagent_type="light"), "cwd": str(self.project)}))
        self.assertEqual(tree.records(), [])


class WhoIsLeftAloneTest(Base):
    def test_a_registered_worker_or_a_native_subagent_starts_nothing(self):
        with mock.patch.object(redirect, "start") as start:
            for env in ({"AGENT_SESSION_ROLE": "worker"}, {"AGENT_DISPATCH_CHILD": "1"},
                        {"AGENT_SESSION_ROLE": "owner"}, {"AGENT_DISPATCH_DEPTH": "1"}):
                with self.subTest(env=env), mock.patch.dict(os.environ, env):
                    self.assertIsNone(redirect.redirect("claude", claude(subagent_type="light")))
            self.assertIsNone(redirect.redirect("claude", {**claude(subagent_type="light"), "agent_id": "a-1"}))
            with mock.patch.object(session_identity, "known_sub_session_root", return_value="root-session"):
                self.assertIsNone(redirect.redirect("codex", codex(agent_type="light")))
            with mock.patch.object(session_identity, "known_sub_session_root", return_value=None):
                self.assertIsNone(redirect.redirect("codex", codex(agent_type="light")))
        start.assert_not_called()

    def test_a_codex_spawn_record_that_cannot_be_read_is_unknown(self):
        with tempfile.TemporaryDirectory() as home:
            env = {"CODEX_HOME": home}
            self.assertIsNone(session_identity.known_sub_session_root("codex", "thread-1", env))
            self.assertEqual(session_identity.sub_session_root("codex", "thread-1", env), "thread-1")
            connection = sqlite3.connect(os.path.join(home, "state_5.sqlite"))
            connection.execute("CREATE TABLE thread_spawn_edges (parent_thread_id TEXT, child_thread_id TEXT)")
            connection.execute("INSERT INTO thread_spawn_edges VALUES ('thread-root', 'thread-child')")
            connection.commit()
            connection.close()
            self.assertEqual(session_identity.known_sub_session_root("codex", "thread-1", env), "thread-1")
            self.assertEqual(session_identity.known_sub_session_root("codex", "thread-child", env), "thread-root")


class StartTest(Base):
    """The start is decided by its receipt or its launch fence, never by a late or missing answer (review 🔴1)."""

    def setUp(self):
        super().setUp()
        self.tree = FakeTree(self)
        os.environ["FAKE_RECORD"] = str(self.tree.record)
        self.withdrawn = []
        patcher = mock.patch.object(redirect, "withdraw",
                                    side_effect=lambda route, path, jobs, api: self.withdrawn.append(route["route_id"]))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.jobs = self.tree.dir / "jobs.log"
        patcher = mock.patch.object(redirect, "_route_api",
                                    return_value=mock.Mock(_compose_default_jobs=lambda: self.jobs))
        patcher.start()
        self.addCleanup(patcher.stop)

    def start(self, mode, call=None, seconds=redirect.BUDGET_SECONDS):
        call = call or redirect.normalized("claude", claude(subagent_type="Explore"))
        with mock.patch.dict(os.environ, {"FAKE_MODE": mode, "CODEX_THREAD_ID": "inherited-thread",
                                          "AGENT_DISPATCH_CALLER_HARNESS": "codex"}):
            return redirect.start("claude", call, "light", os.getcwd(), seconds=seconds)

    def row(self, route_id, **meta):
        fields = {"attempt_schema_version": "2", "attempt_id": "att-h", "route_id": route_id,
                  "route_node": "unit", **meta}
        status = fields.pop("status", "running")
        self.jobs.write_text("\t".join(["2026-11-27T00:00:00Z", status, "/repo", "/repo", "unit",
                                        ",".join(f"{k}={v}" for k, v in fields.items())]) + "\n")

    def test_a_started_route_gives_its_receipt_and_what_the_compose_process_got(self):
        receipt = self.start("running")
        self.assertEqual(receipt["route_id"], "rt-0123456789abcdef")
        [record] = self.tree.records()
        self.assertEqual(record["argv"], ["compose", "--start", "--unit", "general/explore", "--profile", "light",
                                          "--ephemeral", "--prompt-file", "-", "--cwd", os.getcwd()])
        self.assertEqual(record["brief"], "Find the panel rule\n\nWhere is the panel rule?\n")
        self.assertTrue(record["reads_refusal"])          # the compose process reads its own refusal
        self.assertEqual(record["path0"], str(self.tree.dir))
        self.assertFalse(record["cwd_on_path"])           # the project folder is never an import root
        # The session's own identity, as its tool commands carry it: no inherited caller name or other id.
        self.assertEqual(record["env"], {"CLAUDE_CODE_SESSION_ID": SID, "CODEX_THREAD_ID": None,
                                         "OPENCODE_SESSION_ID": None, "AGENT_DISPATCH_CALLER_HARNESS": None})

    def test_codex_names_its_own_session(self):
        call = redirect.normalized("codex", codex(agent_type="explorer"))
        with mock.patch.dict(os.environ, {"FAKE_MODE": "running"}):
            redirect.start("codex", call, "light", os.getcwd())
        env = self.tree.records()[0]["env"]
        self.assertEqual((env["CODEX_THREAD_ID"], env["CLAUDE_CODE_SESSION_ID"]), (SID, None))

    def test_a_receipt_flushed_before_a_slow_finish_is_the_start(self):
        # Start confirmed, then the rest of the output is late, past the budget: the native call is not let go,
        # and the compose process is left to finish on its own.
        began = time.monotonic()
        receipt = self.start("linger", seconds=1)
        self.assertEqual((receipt["route_id"], receipt["parent_next"]), ("rt-linger", "end-turn"))
        self.assertLess(time.monotonic() - began, 5)
        pid = self.tree.pid()
        os.kill(pid, 0)                                    # still running: not stopped after it started
        self.assertEqual(self.withdrawn, [])

    def test_a_route_written_and_then_stalled_is_stopped_and_closed(self):
        began = time.monotonic()
        self.assertIsNone(self.start("route-then-hang", seconds=1))
        self.assertLess(time.monotonic() - began, 10)
        self.assertTrue(gone(self.tree.pid()))             # the whole group went
        self.assertEqual(self.withdrawn, ["rt-hang"])      # and its route was closed, nothing left open

    def test_a_route_past_its_launch_fence_is_the_start_even_without_a_receipt(self):
        self.row("rt-hang", launch_started="1", parent_completion_delivery="claude-parent-runtime")
        receipt = self.start("route-then-hang", seconds=1)
        self.assertEqual((receipt["route_id"], receipt["unit_attempt_id"], receipt["parent_next"]),
                         ("rt-hang", "att-h", "end-turn"))
        self.assertEqual(self.withdrawn, [])

    def test_a_registered_row_that_never_crossed_the_fence_is_no_start(self):
        for meta in ({"launch_claimed": "1"}, {"launch_started": "1", "status": "done"}):
            with self.subTest(meta=meta):
                self.withdrawn.clear()
                self.row("rt-hang", **meta)
                self.assertIsNone(self.start("route-then-hang", seconds=1))
                self.assertEqual(self.withdrawn, ["rt-hang"])

    def test_needs_attention_with_a_registered_attempt_is_settled_by_its_row(self):
        self.row("rt-registered", launch_claimed="1")
        self.assertIsNone(self.start("registered"))
        self.assertEqual(self.withdrawn, ["rt-registered"])
        self.withdrawn.clear()
        self.row("rt-registered", launch_started="1")     # handed over: the start stands, bounded wait
        receipt = self.start("registered")
        self.assertEqual((receipt["route_id"], receipt["parent_next"]), ("rt-registered", "bounded-wait"))
        self.assertTrue(receipt["parent_next_command"].endswith(" --wait"))
        self.assertEqual(self.withdrawn, [])

    def test_the_budget_counts_from_the_input(self):
        # The start is slow to take its input: the budget still ends it (a pipe write would have waited first).
        began = time.monotonic()
        self.assertIsNone(self.start("slow-read", seconds=1))
        self.assertLess(time.monotonic() - began, 2.5)
        self.assertTrue(gone(self.tree.pid()))

    def test_a_large_brief_never_waits_on_a_start_that_does_not_read_it(self):
        call = redirect.normalized("claude", claude(subagent_type="Explore", prompt="x" * 400_000))
        began = time.monotonic()
        self.assertEqual(self.start("no-read", call=call, seconds=5)["route_id"], "rt-0123456789abcdef")
        self.assertLess(time.monotonic() - began, 5)

    def test_every_start_that_did_not_happen_lets_the_native_call_run(self):
        for mode in ("capacity", "refused", "garbage"):
            with self.subTest(mode=mode):
                self.assertIsNone(self.start(mode))
        self.assertEqual(self.withdrawn, [])               # the refused start closed its own route

    def test_a_start_past_its_budget_is_stopped_with_its_group(self):
        began = time.monotonic()
        self.assertIsNone(self.start("hang", seconds=1))
        self.assertLess(time.monotonic() - began, 10)
        self.assertTrue(gone(self.tree.pid()))

    def test_the_start_ends_when_the_hook_process_is_killed(self):
        driver = self.tree.dir / "driver.py"
        driver.write_text(textwrap.dedent(f"""
            import os, sys
            sys.path.insert(0, {str(ROOT / 'utilities')!r})
            from pathlib import Path
            import native_subagent_redirect as r
            r.UTILITIES = Path({str(self.tree.dir)!r})
            call = {{"session": "s", "name": "Explore", "prompt": "p", "title": "", "model": "", "effort": ""}}
            r.start("claude", call, "light", os.getcwd())
        """))
        helper = subprocess.Popen([sys.executable, str(driver)], env={**os.environ, "FAKE_MODE": "hang"},
                                  start_new_session=True)
        self.addCleanup(end_group, helper.pid)
        deadline = time.monotonic() + 20
        while self.tree.pid() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        compose = self.tree.pid()
        self.assertIsNotNone(compose)
        os.kill(helper.pid, signal.SIGKILL)
        helper.wait()
        self.assertTrue(gone(compose))                     # its own group ended it: nothing runs on alone

    def test_a_worker_the_start_launched_does_not_hold_the_hook(self):
        began = time.monotonic()
        self.assertIsNotNone(self.start("late-grandchild"))
        self.assertLess(time.monotonic() - began, 15)
        self.assertTrue(gone(int(self.tree.path(".late").read_text()), 15))

    def test_a_missing_cwd_or_a_broken_payload_runs_natively(self):
        self.assertIsNone(redirect.redirect("claude", {**claude(subagent_type="light"), "cwd": "/nonexistent/x"}))
        self.assertIsNone(redirect.redirect("claude", "not a dict"))
        self.assertIsNone(redirect.redirect("unknown", claude(subagent_type="light")))
        self.assertEqual(self.tree.records(), [])


class ReasonTest(Base):
    """The line follows the start receipt's own way back (review 🔴6)."""

    def test_end_turn_promises_the_notice(self):
        line = redirect.reason_for({"route_id": "rt-1", "state": "running", "parent_next": "end-turn"}, "light")
        self.assertEqual(line, "Started as Hearting unit route rt-1 (general/explore, light, no record). Its answer, "
                               "or what stopped it, comes back to this session as a completion notice. Do not call it "
                               "again; go on with other work or end the turn.")

    def test_bounded_wait_gives_the_wait_command_and_promises_no_notice(self):
        line = redirect.reason_for({"route_id": "rt-1", "state": "running", "parent_next": "bounded-wait",
                                    "parent_next_command": "python3 cr.py start --route r --wait"}, "light")
        self.assertIn("No completion notice reaches this session", line)
        self.assertIn("python3 cr.py start --route r --wait", line)
        self.assertNotIn("as a completion notice", line)
        line = redirect.reason_for({"route_id": "rt-1", "state": "running", "resume_command": "resume"}, "light")
        self.assertIn("run this once to receive its answer: resume", line)

    def test_a_finished_receipt_carries_its_answer(self):
        line = redirect.reason_for({"route_id": "rt-1", "state": "completed", "summary": "Answered: x"}, "light")
        self.assertIn("It has already finished; its answer:\nAnswered: x", line)
        line = redirect.reason_for({"route_id": "rt-1", "state": "completed", "resume_command": "resume"}, "light")
        self.assertIn("read its result once with: resume", line)


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

    def test_the_codex_bridge_and_its_registration(self):
        bridge = ROOT / "adapters/codex/hooks/pretooluse-subagent-redirect.py"
        self.assertTrue(os.access(bridge, os.X_OK))
        self.assertIn("pretooluse-subagent-redirect.py", (ROOT / "adapters/codex/hooks/run-hook.sh").read_text())
        hooks = json.loads((ROOT / "adapters/codex/hooks/hooks.json").read_text())["hooks"]["PreToolUse"]
        [group] = [g for g in hooks if "spawn_agent" in g["matcher"]]
        self.assertEqual(group["matcher"], "spawn_agent|collaborationspawn_agent")
        self.assertIn("pretooluse-subagent-redirect.py", group["hooks"][0]["command"])
        done = subprocess.run([sys.executable, str(bridge)], input=json.dumps(codex(agent_type="light")), text=True,
                              capture_output=True, timeout=60,
                              env={"PATH": "/usr/bin:/bin", "HOME": os.environ.get("HOME", "/tmp"),
                                   "PYTHONDONTWRITEBYTECODE": "1", "AGENT_SESSION_ROLE": "worker"})
        self.assertEqual((done.returncode, done.stdout), (0, ""))

    def test_the_budget_is_inside_every_registration_timeout(self):
        claude_hooks = json.loads((ROOT / "adapters/claude/settings.json").read_text())["hooks"]["PreToolUse"]
        [group] = [g for g in claude_hooks if g["matcher"] == "Agent|Task"]
        self.assertTrue(group["hooks"][0]["command"].endswith("subagent-model-default.sh --redirect"))
        timeouts = [group["hooks"][0]["timeout"]]
        codex_hooks = json.loads((ROOT / "adapters/codex/hooks/hooks.json").read_text())["hooks"]["PreToolUse"]
        timeouts += [g["hooks"][0]["timeout"] for g in codex_hooks if "spawn_agent" in g["matcher"]]
        self.assertEqual(len(timeouts), 2)
        for timeout in timeouts:
            self.assertLess(redirect.BUDGET_SECONDS + 15, timeout)
        plugin = (ROOT / "adapters/opencode/plugins/hearting-guards.js").read_text()
        self.assertNotIn("native_subagent_redirect", plugin)   # OpenCode leaves its subagent calls native


class CodexRunnerTest(unittest.TestCase):
    """`adapters/codex/hooks/run-hook.sh`: no failure of the redirect bridge reaches Codex as exit 2 (review 🔴3)."""

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

    def test_a_call_that_was_not_started_gets_the_model_as_before(self):
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
