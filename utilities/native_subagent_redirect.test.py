#!/usr/bin/env python3
"""A person's session's native subagent call starts as a Hearting unit route, the same on all three harnesses."""
from __future__ import annotations
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])

import io
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
import native_subagent_redirect as redirect  # noqa: E402

SID = "native-subagent-session"
WORKER_ENV = ("AGENT_SESSION_ROLE", "AGENT_DISPATCH_CHILD", "AGENT_DISPATCH_DEPTH", "FLEET_TITLE_REFRESH", "MEM_DISTILL")


def claude(**args):
    return {"session_id": SID, "hook_event_name": "PreToolUse", "tool_name": "Agent", "cwd": os.getcwd(),
            "tool_input": {"description": "Find the panel rule", "prompt": "Where is the panel rule?", **args}}


def codex(tool="spawn_agent", **args):
    return {"session_id": SID, "hook_event_name": "PreToolUse", "tool_name": tool, "cwd": os.getcwd(),
            "tool_input": {"message": "Where is the panel rule?", **args}}


def opencode(tool="subagent", **args):
    return {"tool": tool, "sessionID": SID, "cwd": os.getcwd(),
            "args": {"description": "Find the panel rule", "prompt": "Where is the panel rule?", **args}}


# A stand-in for `capability-route.py`: it records what the hook gave it (argv, the brief, the internal value,
# the session identity) and answers the way FAKE_MODE names.
FAKE_ROUTE = r'''
import json, os, sys, time, subprocess
import work_start
record = {"argv": sys.argv[1:], "brief": sys.stdin.read(), "reads_refusal": work_start.CALLER_READS_REFUSAL,
          "path0": sys.path[0], "cwd_on_path": "" in sys.path or "." in sys.path,
          "env": {k: os.environ.get(k) for k in ("CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID", "OPENCODE_SESSION_ID",
                                                  "AGENT_DISPATCH_CALLER_HARNESS")}}
with open(os.environ["FAKE_RECORD"], "a") as fh:
    fh.write(json.dumps(record) + "\n")
mode = os.environ.get("FAKE_MODE", "running")
if mode == "running":
    print("compose_decision fixture", file=sys.stderr)
    print(json.dumps({"state": "running", "route_id": "rt-0123456789abcdef", "unit_attempt_id": "att-x"}))
elif mode == "late-grandchild":
    # A worker the start launched keeps running (and the output files open) after the start returns.
    late = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(3)"], start_new_session=True)
    with open(os.environ["FAKE_RECORD"] + ".late", "w") as fh:
        fh.write(str(late.pid))
    print(json.dumps({"state": "running", "route_id": "rt-0123456789abcdef"}))
elif mode == "capacity":
    print(json.dumps({"state": "waiting-capacity", "reason": "launch-capacity-wait", "route_id": "rt-1"}))
elif mode == "refused":
    print("capability-route: quick-headless-unavailable", file=sys.stderr)
    raise SystemExit(64)
elif mode == "garbage":
    print("not json")
elif mode == "hang":
    with open(os.environ["FAKE_RECORD"] + ".pid", "w") as fh:
        fh.write(str(os.getpid()))
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

    def records(self):
        return [json.loads(line) for line in self.record.read_text().splitlines()] if self.record.is_file() else []


class Base(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ, {key: "" for key in WORKER_ENV})
        patcher.start()
        self.addCleanup(patcher.stop)
        for key in WORKER_ENV:
            os.environ.pop(key, None)


class NameTableTest(Base):
    def depth(self, harness, payload):
        return redirect.depth_for(harness, redirect.normalized(harness, payload))

    def test_each_name_of_the_table_picks_its_depth_on_every_harness(self):
        table = {"light": "light", "Explore": "light", "explorer": "light", "explore": "light", "memory-scout": "light",
                 "balanced": "balanced", "worker": "balanced", "default": "balanced",
                 "general-purpose": "balanced-deep", "Plan": "balanced-deep", "deep": "deep"}
        for name, depth in table.items():
            self.assertEqual(self.depth("claude", claude(subagent_type=name)), depth, name)
            self.assertEqual(self.depth("codex", codex(agent_type=name)), depth, name)
            self.assertEqual(self.depth("opencode", opencode(agent=name)), depth, name)

    def test_no_name_and_the_opencode_general_agent(self):
        self.assertEqual(self.depth("claude", claude()), "balanced-deep")
        self.assertEqual(self.depth("codex", codex()), "balanced")
        self.assertEqual(self.depth("opencode", opencode()), "balanced")
        self.assertEqual(self.depth("opencode", opencode(agent="general")), "balanced")
        self.assertEqual(self.depth("opencode", opencode("task", subagent_type="general")), "balanced")   # V1
        with self.assertRaises(redirect.Skip):
            self.depth("claude", claude(subagent_type="general"))

    def test_a_named_model_picks_the_depth_it_is_declared_for(self):
        models = {"light": "sonnet", "balanced": "sonnet", "balanced-deep": "opus", "deep": "opus"}
        with mock.patch.object(redirect, "_profile_models", return_value=models):
            self.assertEqual(self.depth("claude", claude(subagent_type="deep", model="opus")), "deep")
            self.assertEqual(self.depth("claude", claude(subagent_type="Explore", model="opus")), "balanced-deep")
            self.assertEqual(self.depth("claude", claude(subagent_type="deep", model="sonnet")), "light")
            with self.assertRaises(redirect.Skip):        # a model no depth declares: the native call keeps it
                self.depth("claude", claude(subagent_type="light", model="haiku"))

    def test_the_shipped_claude_models_name_the_depths(self):
        self.assertEqual(redirect._profile_models("claude"),
                         {"light": "sonnet", "balanced": "sonnet", "balanced-deep": "opus", "deep": "opus"})

    def test_names_out_of_the_table_and_forms_that_carry_history_are_left_alone(self):
        for harness, payload in (
                ("claude", claude(subagent_type="fork")),
                ("claude", claude(subagent_type="web-fetch")),
                ("claude", claude(subagent_type="statusline-setup")),
                ("claude", claude(subagent_type="claude-code-guide")),
                ("claude", claude(subagent_type="claude")),
                ("claude", claude(subagent_type="my-plugin:reviewer")),
                ("claude", claude(subagent_type="light", isolation="worktree")),
                ("claude", claude(subagent_type="light", resume="agent-1")),
                ("claude", claude(subagent_type="light", prompt="")),
                ("codex", codex(agent_type="light", fork_context=True)),
                ("codex", codex("collaborationspawn_agent", agent_type="light", task_name="t")),
                ("codex", codex("collaborationspawn_agent", agent_type="light", task_name="t", fork_turns="all")),
                ("codex", {**codex(agent_type="light"), "tool_input": {"items": [{"type": "text", "text": "x"}]}}),
                ("opencode", opencode(agent="light", session_id="ses_old")),
                ("opencode", opencode(agent="user-reviewer")),
                ("claude", {**claude(subagent_type="light"), "session_id": ""}),
                ("claude", {**claude(subagent_type="light"), "tool_name": "Bash"})):
            with self.subTest(harness=harness, payload=payload):
                with self.assertRaises(redirect.Skip):
                    self.depth(harness, payload)

    def test_codex_v2_starts_only_when_no_history_is_handed_over(self):
        payload = codex("collaborationspawn_agent", agent_type="explorer", task_name="look", fork_turns="none")
        call = redirect.normalized("codex", payload)
        self.assertEqual((call["title"], redirect.depth_for("codex", call)), ("look", "light"))

    def test_the_brief_is_the_title_and_the_prompt(self):
        call = redirect.normalized("claude", claude(subagent_type="light"))
        self.assertEqual(redirect.brief(call), "Find the panel rule\n\nWhere is the panel rule?\n")
        call = redirect.normalized("codex", codex(agent_type="light"))
        self.assertEqual(redirect.brief(call), "Where is the panel rule?\n")


class WhoIsLeftAloneTest(Base):
    def test_a_registered_worker_or_a_native_subagent_starts_nothing(self):
        with mock.patch.object(redirect, "start") as start:
            for env in ({"AGENT_SESSION_ROLE": "worker"}, {"AGENT_DISPATCH_CHILD": "1"},
                        {"AGENT_SESSION_ROLE": "owner"}, {"AGENT_DISPATCH_DEPTH": "1"}):
                with self.subTest(env=env), mock.patch.dict(os.environ, env):
                    self.assertIsNone(redirect.redirect("claude", claude(subagent_type="light")))
            self.assertIsNone(redirect.redirect("claude", {**claude(subagent_type="light"), "agent_id": "a-1"}))
            import session_identity
            with mock.patch.object(session_identity, "sub_session_root", return_value="root-session"):
                self.assertIsNone(redirect.redirect("codex", codex(agent_type="light")))
        start.assert_not_called()


class StartTest(Base):
    def setUp(self):
        super().setUp()
        self.tree = FakeTree(self)
        os.environ["FAKE_RECORD"] = str(self.tree.record)

    def run_redirect(self, harness, payload, mode, seconds=redirect.BUDGET_SECONDS):
        with mock.patch.dict(os.environ, {"FAKE_MODE": mode, "CODEX_THREAD_ID": "inherited-thread",
                                          "AGENT_DISPATCH_CALLER_HARNESS": "codex"}):
            return redirect.redirect(harness, payload, seconds=seconds)

    def test_a_started_route_refuses_the_native_call_with_one_line(self):
        reason = self.run_redirect("claude", claude(subagent_type="Explore"), "running")
        self.assertEqual(reason, redirect.reason_for("rt-0123456789abcdef", "light"))
        self.assertIn("Started as Hearting unit route rt-0123456789abcdef (general/explore, light, no record)", reason)
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

    def test_each_harness_names_its_own_session(self):
        self.run_redirect("codex", codex(agent_type="explorer"), "running")
        self.run_redirect("opencode", opencode(agent="explore"), "running")
        envs = [r["env"] for r in self.tree.records()]
        self.assertEqual((envs[0]["CODEX_THREAD_ID"], envs[0]["CLAUDE_CODE_SESSION_ID"]), (SID, None))
        self.assertEqual((envs[1]["OPENCODE_SESSION_ID"], envs[1]["CODEX_THREAD_ID"]), (SID, None))

    def test_every_start_that_did_not_happen_lets_the_native_call_run(self):
        for mode in ("capacity", "refused", "garbage"):
            with self.subTest(mode=mode):
                self.assertIsNone(self.run_redirect("claude", claude(subagent_type="light"), mode))

    def test_a_start_past_its_budget_is_stopped_and_the_native_call_runs(self):
        began = time.monotonic()
        self.assertIsNone(self.run_redirect("claude", claude(subagent_type="light"), "hang", seconds=1))
        self.assertLess(time.monotonic() - began, 10)
        pid = int(Path(str(self.tree.record) + ".pid").read_text())
        with self.assertRaises(ProcessLookupError):        # the late start was ended with its process group
            os.kill(pid, 0)

    def test_a_worker_the_start_launched_does_not_hold_the_hook(self):
        began = time.monotonic()
        self.assertIsNotNone(self.run_redirect("claude", claude(subagent_type="light"), "late-grandchild"))
        self.assertLess(time.monotonic() - began, 15)
        late = int(Path(str(self.tree.record) + ".late").read_text())
        for _ in range(100):                               # it ends by itself; leave nothing behind
            try:
                os.kill(late, 0)
            except ProcessLookupError:
                break
            time.sleep(0.1)

    def test_a_missing_cwd_or_a_broken_payload_runs_natively(self):
        self.assertIsNone(redirect.redirect("claude", {**claude(subagent_type="light"), "cwd": "/nonexistent/x"}))
        self.assertIsNone(redirect.redirect("claude", "not a dict"))
        self.assertIsNone(redirect.redirect("unknown", claude(subagent_type="light")))
        self.assertEqual(self.tree.records(), [])


class EntryTest(Base):
    def test_main_prints_the_deny_json_or_the_opencode_reason_and_never_fails(self):
        line = redirect.reason_for("rt-1", "light")
        with mock.patch.object(redirect, "redirect", return_value=line):
            out = io.StringIO()
            with mock.patch.object(sys, "stdout", out):
                self.assertEqual(redirect.main(["--claude"], io.StringIO(json.dumps(claude()))), 0)
            decision = json.loads(out.getvalue())["hookSpecificOutput"]
            self.assertEqual((decision["permissionDecision"], decision["permissionDecisionReason"]), ("deny", line))
            out = io.StringIO()
            with mock.patch.object(sys, "stdout", out):
                self.assertEqual(redirect.main(["--opencode"], io.StringIO("{}")), 1)
            self.assertEqual(out.getvalue().strip(), line)
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
        plugin = (ROOT / "adapters/opencode/plugins/hearting-guards.js").read_text()
        timeouts.append(int(re.search(r"const subagentRedirectMs = (\d+)", plugin).group(1)) / 1000)
        self.assertEqual(len(timeouts), 3)
        for timeout in timeouts:
            self.assertLess(redirect.BUDGET_SECONDS + 15, timeout)


class ClaudeHookTest(Base):
    """`hooks/subagent-model-default.sh`: with `--redirect` the redirect comes first; without it (the line a
    session opened before this change was registered with) nothing is redirected."""

    def run_hook(self, args, payload, reason):
        tree = Path(tempfile.mkdtemp(prefix="nsr-hook-"))
        self.addCleanup(shutil.rmtree, tree, True)
        (tree / "hooks").mkdir()
        (tree / "utilities").mkdir()
        shutil.copy(ROOT / "hooks/subagent-model-default.sh", tree / "hooks/subagent-model-default.sh")
        (tree / "config").mkdir()
        (tree / "config/models.conf").write_text("CFG_TIER_LIGHT_MODEL=sonnet\nCFG_NATIVE_SUBAGENT=light\n"
                                                 "CFG_MAIN_SESSION_ONLY_MODELS=\"fable\"\n")
        (tree / "utilities/native_subagent_redirect.py").write_text(
            "def redirect(harness, payload):\n"
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

    def test_a_fork_is_still_refused_by_the_model_policy(self):
        out = self.run_hook(["--redirect"], claude(subagent_type="fork"), None)
        self.assertEqual(out["permissionDecisionReason"], "native-subagent-model-inheritance-ineligible")


if __name__ == "__main__":
    unittest.main()
