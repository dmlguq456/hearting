#!/usr/bin/env python3
"""A worker's first prompt carries the user's work-style records for its unit and field (design 12c).

Real wrapper prompt assembly for all three harnesses, a real ``mem`` over a throwaway store. Each scene
is a worker prompt the wrapper builds; the fixture records are invented.
"""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import importlib.util
import json
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "utilities"))
MEM = ROOT / "tools" / "memory" / "mem.py"
HARNESSES = ("claude", "codex", "opencode")
SECTION = "Unit bootstrap material (runtime-supplied, read-only reference):"

RECORDS = (   # (body, topics, headline)
    ("WS-UNIT keep review comments to blocking issues first.", ("unit:qa/code-review",), "review comment style"),
    ("WS-FIELD-CODE name the failing command before the fix.", ("field:code",), "code habit"),
    ("WS-FIELD-DOCS lead every section with its claim.", ("field:docs",), "docs habit"),
    ("WS-FRAME-UNIT lead with the intent in one sentence.", ("unit:plan/frame",), "frame habit"),
    ("WS-UNTAGGED a preference carrying no label at all.", (), "untagged habit"),
    ("WS-REPORT-UNIT report layout: one table per result.", ("unit:editorial/report",), "report format layout"),
    ("WS-REPORT-QUERY report format: html tables, not prose.", (), "report format html"),
)


def _wrapper(harness):
    spec = importlib.util.spec_from_file_location(
        f"{harness}_work_style_fixture", ROOT / f"adapters/{harness}/bin/dispatch-headless.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class WorkStylePromptTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="ws-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        (self.repo / "x").write_text("x")
        self.art = self.tmp / ".agent_reports"
        self.art.mkdir()
        self.store = self.tmp / "store"
        self.store.mkdir()
        patcher = mock.patch.dict(os.environ, {"MEM_STORE": str(self.store)})
        patcher.start()
        self.addCleanup(patcher.stop)
        for body, topics, headline in RECORDS:
            cmd = [sys.executable, str(MEM), "add", "durable", "preference", body, "--scope", "global",
                   "--headline", headline]
            for topic in topics:
                cmd += ["--topic", topic]
            subprocess.run(cmd, cwd=self.repo, check=True, capture_output=True, env=dict(os.environ))

    def route(self, nodes=None, field="code"):
        path = self.tmp / f"route{len(list(self.tmp.glob('route*.json')))}.json"
        route = {"nodes": nodes or [{"id": "plan", "dispatch_depth": 2}]}
        if field:
            route["field"] = field
        path.write_text(json.dumps(route), encoding="utf-8")
        return path

    def prompt(self, harness, *, unit, worker_type, depth=2, node="impl-review", route=None, extra=(),
               before=False):
        """The prompt the wrapper builds; ``before`` is the same prompt with the work-style section never added."""
        wrapper = _wrapper(harness)
        if before:
            wrapper.unit_bootstrap_prompt = lambda *a, **k: ""
        argv = ["--worktree", str(self.repo), "--slug", "ws", "--capability", "autopilot-code",
                "--capability-mode", "dev", "--worker-type", worker_type, "--dispatch-depth", str(depth),
                "--owner", "autopilot-code", "--assigned-contract", "code-execute", "--route-node", node,
                "--attempt-id", "att-ws", "--foreground-timeout", "600", "--prompt-text", "do the assigned work",
                "--qa", "quick", "--intensity", "quick", *extra]
        if unit:
            argv += ["--unit", unit]
        if route is not None:
            argv += ["--route-file", str(route)]
        args = wrapper.parser().parse_args(argv)
        args.artifact_root = str(self.art)
        if not getattr(args, "agent_home", None):
            args.agent_home = str(self.tmp / "agent-home")
        if harness == "opencode":
            return wrapper.prompt(args)[0], wrapper
        return wrapper.dispatch_prompt(args, ("do the assigned work", "inline"))[0], wrapper

    def test_a_staged_unit_gets_its_unit_and_field_records_first_and_only_those(self):
        route = self.route()
        seen = {}
        for harness in HARNESSES:
            prompt, _ = self.prompt(harness, unit="qa/code-review", worker_type="review", route=route)
            seen[harness] = prompt
            self.assertEqual(prompt.count(SECTION), 1, harness)
            section = prompt[prompt.index(SECTION):prompt.index("Assignment:")]
            self.assertIn("WS-UNIT", section)
            self.assertIn("WS-FIELD-CODE", section)
            self.assertLess(section.index("WS-UNIT"), section.index("WS-FIELD-CODE"))
            for absent in ("WS-FIELD-DOCS", "WS-UNTAGGED", "WS-REPORT"):
                self.assertNotIn(absent, prompt)
            self.assertIn("do not write, curate, or sync memory", section)
            self.assertNotIn("format follows", section)
            self.assertLess(prompt.index("Unit: qa/code-review"), prompt.index(SECTION))
        sections = {h: p[p.index(SECTION):p.index("Assignment:")] for h, p in seen.items()}
        self.assertEqual(len(set(sections.values())), 1)

    def test_an_unmoved_unit_and_a_frame_leg_get_the_field_records_and_their_own_unit_label(self):
        route = self.route()
        for harness in HARNESSES:
            prompt, _ = self.prompt(harness, unit="dev/frontend", worker_type="stage", route=route, node="execute")
            self.assertIn("WS-FIELD-CODE", prompt, harness)
            self.assertNotIn("WS-UNIT", prompt)
            frame, _ = self.prompt(harness, unit=None, worker_type="frame", depth=1, node="frame", route=route)
            self.assertIn("WS-FIELD-CODE", frame, harness)
            self.assertEqual(frame.count(SECTION), 1)
            # a real frame route seals no field: only the unit-labelled record arrives
            sealed = self.route(field=None)
            frame, _ = self.prompt(harness, unit=None, worker_type="frame", depth=1, node="frame", route=sealed)
            self.assertIn("WS-FRAME-UNIT", frame, harness)
            self.assertNotIn("WS-FIELD-CODE", frame)
            self.assertEqual(frame.count(SECTION), 1)

    def test_a_one_shot_owner_gets_the_field_records_and_a_conductor_none(self):
        one_shot = self.route(nodes=[{"id": "one-shot", "dispatch_depth": 1, "unit": "_kernel/owner"}])
        conductor = self.route(nodes=[{"id": "plan", "dispatch_depth": 2}])
        for harness in HARNESSES:
            solo, _ = self.prompt(harness, unit="_kernel/owner", worker_type="owner", depth=1, node="one-shot",
                                  route=one_shot)
            self.assertIn("WS-FIELD-CODE", solo, harness)
            self.assertNotIn("WS-UNIT", solo)
            self.assertLess(solo.index(SECTION), solo.index("Assignment:"))
            lead, _ = self.prompt(harness, unit=None, worker_type="owner", depth=1, node="", route=conductor)
            self.assertNotIn(SECTION, lead, harness)
            self.assertNotIn("WS-FIELD-CODE", lead)

    def test_a_declared_unit_gets_old_and_new_records_once(self):
        route = self.route()
        for harness in HARNESSES:
            prompt, _ = self.prompt(harness, unit="editorial/report", worker_type="stage", route=route, node="report")
            self.assertEqual(prompt.count("WS-REPORT-UNIT"), 1, harness)
            self.assertEqual(prompt.count("WS-REPORT-QUERY"), 1, harness)
            self.assertIn("WS-FIELD-CODE", prompt)
            self.assertLess(prompt.index("WS-REPORT-UNIT"), prompt.index("WS-REPORT-QUERY"))
            self.assertIn("format follows these conventions", prompt)

    def test_no_record_or_a_failing_mem_leaves_the_prompt_byte_for_byte_as_it_was(self):
        route = self.route(field="lab")            # no record carries unit qa/test or field lab
        broken = self.tmp / "broken-store"
        broken.write_text("not a directory")
        for harness in HARNESSES:
            for store in (None, broken, self.tmp / "missing"):
                env = {"MEM_STORE": str(store)} if store else {}
                with mock.patch.dict(os.environ, env):
                    now, _ = self.prompt(harness, unit="qa/test", worker_type="review", route=route, node="test")
                    before, _ = self.prompt(harness, unit="qa/test", worker_type="review", route=route, node="test",
                                            before=True)
                self.assertEqual(now, before, (harness, store))
                self.assertNotIn(SECTION, now)
        import worker_bootstrap as wb
        args = type("A", (), {"worker_type": "review", "unit": "qa/test", "route_file": str(route),
                              "worktree": str(self.repo), "completion_gate": None})()
        started = time.monotonic()
        with mock.patch("subprocess.run", side_effect=OSError("no mem")):
            self.assertEqual(wb.unit_bootstrap_prompt(args, "t", dict(os.environ)), "")
        self.assertLess(time.monotonic() - started, 1)


if __name__ == "__main__":
    unittest.main()
