#!/usr/bin/env python3
"""The advice line of a directly opened route: "방향 잡기 권함" (routing/prd.md R16).

Real compose and the real registry. A `solo` or `staged` route whose request shows a sign that
settling the direction first is worth it (a protected path, two or more adapters, a spec or ship
kind) gets one card line and one `frame_advice=` line. It is
advice only: the route opens the same either way. A unit route, `direct`, a framed route and a route
made from a proposal get none.
"""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import importlib.util
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "tools"))


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, HERE / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


F = _load("framed_route_for_advice", "framed_route.test.py")
T, R = F.T, F.R
import capability_route_scope as SCOPE  # noqa: E402

ADVICE_KO = "방향 잡기 권함"
CLEAR = "Rename the helper in utilities/x.py and update its callers"
PROTECTED = "Reword the rule in core/WORKFLOW.md"
ADAPTERS = "Align adapters/claude/a.py with adapters/codex/b.py"


class FrameAdviceTest(F.FramedBase):
    def route(self, text, **kw):
        arguments = dict(capability="autopilot-code", shape="solo", slug="advice-fixture",
                         registered_headless_evidence=T.registered_headless(),
                         work_request={"text": text, "owner_harness": None})
        arguments.update(kw)
        return self.compose(**arguments)

    def card(self, route):
        return R.compose_card(route, include_decisions=False)

    def test_a_clear_request_gets_no_advice_and_the_card_is_unchanged(self):
        for shape in ("solo", "staged"):
            with self.subTest(shape=shape):
                route = self.route(CLEAR, shape=shape)
                self.assertEqual(SCOPE.frame_advice(route), [])
                self.assertNotIn(ADVICE_KO, self.card(route))

    def test_advice_stays_inside_a_hearting_checkout(self):
        # Another project's `core/` is its own directory, and the ship kind is not
        # Hearting's direction to settle; the same request outside a Hearting
        # checkout gets no advice line.
        protected = {**self.route(PROTECTED), "cwd": str(self.root)}
        self.assertEqual(SCOPE.frame_advice(protected), [])
        self.assertNotIn(ADVICE_KO, self.card(protected))
        ship = {**self.route(CLEAR, capability="autopilot-ship"), "cwd": str(self.root)}
        self.assertEqual(SCOPE.frame_advice(ship), [])

    def test_a_hearting_worktree_keeps_the_advice(self):
        worktree = Path(self.root) / "hearting-wt"
        for marker in ("utilities/capability-route.py", "core/WORKFLOW.md", "capabilities/README.md"):
            (worktree / marker).parent.mkdir(parents=True, exist_ok=True)
            (worktree / marker).write_text("", encoding="utf-8")
        route = {**self.route(PROTECTED), "cwd": str(worktree)}
        self.assertEqual(SCOPE.frame_advice(route), [("protected-path", "core/WORKFLOW.md")])

    def test_each_sign_adds_one_line_and_names_its_reason(self):
        cases = (
            ("protected path", PROTECTED, {}, [("protected-path", "core/WORKFLOW.md")]),
            ("two adapters", ADAPTERS, {}, [("adapters", "claude,codex")]),
            ("spec kind", CLEAR, {"capability": "autopilot-spec"}, [("capability", "autopilot-spec")]),
            ("ship kind", CLEAR, {"capability": "autopilot-ship"},
             [("capability", "autopilot-ship")]),
        )
        for shape in ("solo", "staged"):
            for name, text, extra, signals in cases:
                with self.subTest(shape=shape, case=name):
                    route = self.route(text, shape=shape, **extra)
                    self.assertEqual(SCOPE.frame_advice(route), signals)
                    advice = [line for line in self.card(route).splitlines() if ADVICE_KO in line]
                    self.assertEqual(len(advice), 1, advice)   # one line, however many signs
                    self.assertTrue(advice[0].strip().startswith(ADVICE_KO + ": "))

    def test_a_korean_particle_after_a_path_is_not_part_of_the_reason(self):
        route = self.route("core/WORKFLOW.md의 규칙 문장을 한 줄 고친다")
        self.assertEqual(SCOPE.frame_advice(route), [("protected-path", "core/WORKFLOW.md")])

    def test_a_start_approval_alone_is_not_a_sign(self):
        route = self.route(CLEAR, capability="autopilot-lab", capability_mode="setup")
        self.assertTrue(SCOPE.route_start_approvals(route))      # the card shows the approval itself
        self.assertEqual(SCOPE.frame_advice(route), [])
        self.assertNotIn(ADVICE_KO, self.card(route))

    def test_the_machine_line_and_the_card_say_the_same_reason(self):
        route = self.route(PROTECTED)
        signals = SCOPE.frame_advice(route)
        self.assertEqual(SCOPE.frame_advice_reason(signals), "protected-path:core/WORKFLOW.md")
        self.assertIn(ADVICE_KO + ": 과제에 core/WORKFLOW.md", self.card(route))

    def test_the_route_is_the_same_with_or_without_the_sign(self):
        plain = self.route(CLEAR)
        signed = self.route(PROTECTED)
        for route in (plain, signed):
            self.assertEqual((route["capability"], route["selection"]["shape"], route["effective_intensity"]),
                             ("autopilot-code", "solo", "quick"))
            self.assertEqual(route.get("human_gates") or [], plain.get("human_gates") or [])
            self.assertEqual([n["id"] for n in route["nodes"]], [n["id"] for n in plain["nodes"]])

    def test_the_other_shapes_get_no_advice(self):
        self.assertEqual(SCOPE.frame_advice(self.route(PROTECTED, shape="direct")), [])
        framed = self.compose(work_request={"text": PROTECTED, "owner_harness": None})
        self.assertEqual(SCOPE.frame_advice(framed), [])
        self.assertNotIn(ADVICE_KO, self.card(framed))

    def test_a_route_made_from_a_proposal_gets_no_advice(self):
        route = {**self.route(PROTECTED), "route_plan": {"record": "x", "leg": 0}}
        self.assertEqual(SCOPE.frame_advice(route), [])

    def test_a_route_that_carries_its_own_frame_legs_gets_no_advice(self):
        route = self.route(PROTECTED, capability="autopilot-spec", shape="staged")
        self.assertTrue(SCOPE.frame_advice(route))                  # composed without the recipe's frame pair
        leg = {"id": "frame", "worker_type": "frame", "dispatch_depth": 1, "launch_authority": "depth-0"}
        self.assertEqual(SCOPE.frame_advice({**route, "nodes": [leg, *route["nodes"]]}), [])

    def test_a_route_sealed_before_the_work_request_was_kept_gets_no_advice(self):
        route = self.route(PROTECTED)
        route = {key: value for key, value in route.items() if key != "work_request"}
        self.assertEqual(SCOPE.frame_advice(route), [])


class FrameAdviceCliTest(F.FramedBase):
    def run_cli(self, text, *extra, language=None):
        brief = Path(self._tmp.name) / "brief.md"
        brief.write_text(text + "\n", encoding="utf-8")
        evidence = Path(self._tmp.name) / "headless.json"
        evidence.write_text(json.dumps(T.registered_headless()), encoding="utf-8")
        home = Path(self._tmp.name) / "home"
        env = {**F.clean_environment(), "AGENT_HOME": str(R.ROOT), "AGENT_DISPATCH_JOBS": str(self.jobs),
               "FLEET_ROUTE_CHAIN_DIR": str(Path(self._tmp.name) / "chains"), "HOME": str(home),
               "XDG_CONFIG_HOME": str(home / "xdg"), "CLAUDE_CONFIG_DIR": str(home / "claude")}
        env.pop("FLEET_NOW_LANG", None)
        if language:
            env["FLEET_NOW_LANG"] = language
        return subprocess.run(
            [sys.executable, str(HERE / "capability-route.py"), "compose", "--cwd", str(R.ROOT),
             "--artifact-root", str(self.root), "--spec-read", "fixture", "--prompt-file", str(brief),
             "--registered-headless-evidence", str(evidence), "--unassigned", "--explain", *extra],
            text=True, capture_output=True, env=env, check=False)

    def test_a_sign_prints_the_line_and_the_route_still_opens(self):
        opened = self.run_cli(PROTECTED, "--capability", "autopilot-code", "--shape", "solo", language="en")
        self.assertEqual(opened.returncode, 0, opened.stderr)
        self.assertIn("frame_advice=protected-path:core/WORKFLOW.md\n", opened.stderr)
        self.assertIn("Direction check advised: the request names core/WORKFLOW.md", opened.stderr)
        self.assertIn("route_file_written=0 explain=1", opened.stderr)
        self.assertEqual(json.loads(opened.stdout.strip().splitlines()[-1])["shape"], "solo")

    def test_the_line_follows_the_declared_language(self):
        opened = self.run_cli(PROTECTED, "--capability", "autopilot-code", "--shape", "solo", language="ko")
        self.assertEqual(opened.returncode, 0, opened.stderr)
        self.assertIn("방향 잡기 권함: 과제에 core/WORKFLOW.md", opened.stderr)

    def test_no_sign_prints_neither_line(self):
        opened = self.run_cli(CLEAR, "--capability", "autopilot-code", "--shape", "solo", language="en")
        self.assertEqual(opened.returncode, 0, opened.stderr)
        self.assertNotIn("frame_advice=", opened.stderr)
        self.assertNotIn("Direction check advised", opened.stderr)

    def test_a_unit_route_gets_no_advice(self):
        opened = self.run_cli(PROTECTED, "--unit", "qa/code-review", language="en")
        self.assertEqual(opened.returncode, 0, opened.stderr)
        self.assertNotIn("frame_advice=", opened.stderr)
        self.assertNotIn("Direction check advised", opened.stderr)


if __name__ == "__main__":
    unittest.main()
