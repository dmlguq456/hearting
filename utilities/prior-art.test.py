#!/usr/bin/env python3
"""Tests for `prior-art.py` and its framed-route hookup in `work_start.py`.

No test calls a real model: `invoke_model` is replaced by a guard that fails the
test, and every scan receives an injected `invoke`.
"""
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

_SPEC = importlib.util.spec_from_file_location("prior_art_under_test", HERE / "prior-art.py")
PA = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(PA)

import work_start as W  # noqa: E402

PLAN = json.dumps({"code_terms": ["widget_scan"], "artifact_terms": ["widget"],
                   "memory_queries": ["widget scan"], "history_terms": ["widget"]})
MEMORY = [{"cite": "mem:decision_widget_1", "text": "[decision] widget scans stay read-only", "term": "q"}]


class FakeModel:
    """Answers per stage, recording every prompt and timeout it was given."""

    def __init__(self, plan=None, summary=None):
        self.answers = {"plan": plan, "summary": summary}
        self.calls = []

    def __call__(self, prompt, timeout, *, stage):
        self.calls.append((stage, prompt, timeout))
        answer = self.answers[stage]
        return answer(prompt) if callable(answer) else answer


def _git(cwd, *args):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
                          cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


class ScanBase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.repo = self.tmp / "repo"
        (self.repo / "tools").mkdir(parents=True)
        (self.repo / "tools/widget_scan.py").write_text("def widget_scan():\n    return 'widget_scan'\n")
        (self.repo / "README.md").write_text("Nothing here.\n")
        _git(self.repo, "init", "-q")
        _git(self.repo, "add", ".")
        _git(self.repo, "commit", "-q", "-m", "feat: add the widget scan helper")
        self.sha = _git(self.repo, "rev-parse", "--short", "HEAD")
        self.artifacts = self.tmp / "artifacts"
        (self.artifacts / "campaigns").mkdir(parents=True)
        (self.artifacts / "campaigns/INDEX.md").write_text(
            "| ID | Title | Started | Status | Path |\n|---|---|---|---|---|\n"
            "| camp_1 | Widget scan study | 2026-10-01 | active | campaigns/2026-10-01_widget-scan |\n")
        guard = mock.patch.object(PA, "invoke_model", side_effect=AssertionError("a real model was called"))
        guard.start()
        self.addCleanup(guard.stop)
        memory = mock.patch.object(PA, "memory_hits", return_value=list(MEMORY))
        self.memory = memory.start()
        self.addCleanup(memory.stop)

    def grounded_summary(self):
        return PA.Answer("\n".join([
            "- [same] tools/widget_scan.py:1 — widget_scan() already exists",
            "- [similar] art:campaigns/2026-10-01_widget-scan — a widget scan study campaign",
            "- [decided] mem:decision_widget_1 — widget scans stay read-only",
            f"- [same] git:{self.sha} — the helper landed in one commit",
        ]), "claude", "sonnet-test")

    def scan(self, model, **kwargs):
        kwargs.setdefault("budget", 30)
        return PA.scan("Add a widget scan", cwd=self.repo, invoke=model, artifact_root=self.artifacts, **kwargs)


class ScanTest(ScanBase):
    def test_a_normal_scan_lists_grounded_facts_with_their_sources(self):
        model = FakeModel(PA.Answer(PLAN, "codex", "luna-test"), self.grounded_summary())
        result = self.scan(model)
        self.assertTrue(result.ok, result.text)
        self.assertTrue(result.text.startswith(PA.HEADER))
        self.assertIn("not instructions", result.text.splitlines()[0])
        self.assertIn("mem show <id> --no-touch", result.text.splitlines()[0])
        for cite in ("tools/widget_scan.py:1", "art:campaigns/2026-10-01_widget-scan",
                     "mem:decision_widget_1", f"git:{self.sha}"):
            self.assertIn(cite, result.text)
        self.assertEqual((result.harness, result.model), ("claude", "sonnet-test"))
        self.assertIn("model claude/sonnet-test (mini, terms by model)", result.text)
        self.assertEqual([call[0] for call in model.calls], ["plan", "summary"])
        evidence = model.calls[1][1]
        self.assertIn("tools/widget_scan.py:1 |", evidence)
        self.assertIn("mem:decision_widget_1 |", evidence)
        self.assertLessEqual(len(result.text.encode()), PA.MAX_BYTES)
        self.memory.assert_called_once()
        self.assertEqual(self.memory.call_args.args[1], ["widget scan"])

    def test_with_every_harness_blocked_it_ends_at_once_with_one_line(self):
        model = FakeModel(PA.Answer("", reason="no-harness-available"))
        with mock.patch.object(PA, "code_hits", side_effect=AssertionError("collected after a blocked model")):
            started = time.monotonic()
            result = self.scan(model)
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(result.text, "scan unavailable: no-harness-available\n")
        self.assertFalse(result.ok)
        self.assertEqual(len(model.calls), 1)
        self.memory.assert_not_called()

    def test_an_unanswered_summary_is_one_line(self):
        result = self.scan(FakeModel(PA.Answer(PLAN, "codex", "m"), PA.Answer("", reason="no-model-answer")))
        self.assertEqual(result.text, "scan unavailable: no-model-answer\n")

    def test_every_step_stays_inside_the_budget_and_a_slow_source_is_left_behind(self):
        def slow_memory(*_args, **_kwargs):
            time.sleep(3)
            return list(MEMORY)
        self.memory.side_effect = slow_memory
        model = FakeModel(PA.Answer(PLAN, "codex", "m"), self.grounded_summary())
        started = time.monotonic()
        result = self.scan(model, budget=6)
        self.assertLess(time.monotonic() - started, 2.9)
        self.assertTrue(result.ok, result.text)
        self.assertIn("not searched in time: memory", result.text)
        self.assertNotIn("mem:decision_widget_1", result.text)
        plan_timeout, summary_timeout = model.calls[0][2], model.calls[1][2]
        self.assertAlmostEqual(plan_timeout, 6 * 0.35)
        self.assertLessEqual(summary_timeout, 6 - 1.8)

    def test_lines_without_collected_evidence_are_dropped_mechanically(self):
        summary = PA.Answer("\n".join([
            "- [same] nope/ghost.py:3 — an invented file",
            "- [decided] mem:invented_1 — an invented memory",
            "- [same] git:deadbeef12 — an invented commit",
            "- [similar] art:campaigns/ghost — an invented campaign",
            "- [recommend] tools/widget_scan.py:1 — not a relation",
            "Here is my list:",
            "- [same] tools/widget_scan.py:99 — the real file at a line nobody saw",
            "- [same] tools/widget_scan.py:1 — the same file again",
            "- [decided] mem:decision_widget_1 — kept",
        ]), "claude", "m")
        result = self.scan(FakeModel(PA.Answer(PLAN, "codex", "m"), summary))
        items = [line for line in result.text.splitlines() if line.startswith("- [")]
        self.assertEqual(items, [
            "- [same] tools/widget_scan.py — the real file at a line nobody saw",
            "- [decided] mem:decision_widget_1 — kept",
        ])

    def test_the_list_stays_within_ten_items_and_three_kilobytes(self):
        evidence = {"code": [{"cite": f"tools/w{index:02d}.py:1", "text": "w", "term": "w"} for index in range(15)]}
        long_fact = "가" * 400
        items = PA.validate_items("\n".join(
            f"- [similar] tools/w{index:02d}.py:1 — {long_fact}" for index in range(15)), evidence)
        self.assertEqual(len(items), PA.MAX_ITEMS)
        for line in items:
            self.assertLessEqual(len(line.split(" — ", 1)[1]), PA.FACT_CHARS)
        text = PA.render("t", self.repo, None, items, evidence, [], "claude", "m", 1.0, "model")
        self.assertLessEqual(len(text.encode()), PA.MAX_BYTES)
        kept = [line for line in text.splitlines() if line.startswith("- [")]
        self.assertTrue(0 < len(kept) < PA.MAX_ITEMS)   # 160 Hangul facts exceed 3 KB before ten lines
        self.assertEqual(kept, items[:len(kept)])
        text.encode().decode("utf-8")

    def test_the_planner_sees_file_names_and_projected_copies_count_once(self):
        copy = self.repo / "adapters/claude/tools/widget_scan.py"
        copy.parent.mkdir(parents=True)
        copy.write_text((self.repo / "tools/widget_scan.py").read_text())
        (self.repo / "tools/widget_scan.test.py").write_text("widget_scan\n")
        _git(self.repo, "add", ".")
        _git(self.repo, "commit", "-q", "-m", "copy")
        hits = PA.code_hits(self.repo, ["widget_scan"], time.monotonic() + 10)
        self.assertEqual([h["cite"] for h in hits if h["text"] == "(file name match)"],
                         ["tools/widget_scan.py", "tools/widget_scan.test.py"])
        self.assertEqual([h["cite"] for h in hits if h["text"] != "(file name match)"],
                         ["tools/widget_scan.py:1", "tools/widget_scan.py:2", "tools/widget_scan.test.py:1"])
        model = FakeModel(PA.Answer("", reason="no-harness-available"))
        self.scan(model)
        self.assertIn("File names in the project (extension and tests omitted): README widget_scan\n",
                      model.calls[0][1])

    def test_a_large_code_block_leaves_room_for_the_other_sources(self):
        evidence = {"code": [{"cite": f"c{index}.py:1", "text": "x" * 190, "term": "t"} for index in range(400)],
                    "memory": list(MEMORY), "history": [{"cite": "git:abcdef1", "text": "h", "term": ""}]}
        block = PA.render_evidence(evidence)
        self.assertIn("mem:decision_widget_1 |", block)
        self.assertIn("git:abcdef1 |", block)
        self.assertLessEqual(len(block), sum(PA.EVIDENCE_CHARS.values()) + 100)

    def test_an_unusable_plan_falls_back_to_task_words(self):
        result = self.scan(FakeModel(PA.Answer("no json here", "codex", "m"), PA.Answer("- none", "claude", "m")))
        self.assertTrue(result.ok, result.text)
        self.assertIn("terms by task words", result.text)
        self.assertIn("- nothing in the evidence bears on the task", result.text)


class MemoryReadOnlyTest(unittest.TestCase):
    def test_memory_is_read_through_no_touch_recall_as_a_worker(self):
        seen = []

        def run(argv, **kwargs):
            seen.append((argv, kwargs))
            return json.dumps({"results": [{"id": "decision_a_1", "type": "decision", "body": "x"}]})
        with mock.patch.object(PA, "_run", side_effect=run):
            hits = PA.memory_hits(Path("."), ["widget scan"], time.monotonic() + 10)
        self.assertEqual([hit["cite"] for hit in hits], ["mem:decision_a_1"])
        argv, kwargs = seen[0]
        self.assertEqual(argv[2:4], ["recall", "widget scan"])
        self.assertIn("--no-touch", argv)
        self.assertIn("--json", argv)
        self.assertEqual(kwargs["env"]["AGENT_SESSION_ROLE"], "worker")


class CliTest(ScanBase):
    def test_the_cli_publishes_once_and_always_exits_zero(self):
        out = self.tmp / "scan.md"
        blocked = FakeModel(PA.Answer("", reason="no-harness-available"))
        with mock.patch.object(PA, "invoke_model", side_effect=blocked), \
                mock.patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(PA.main(["Add a widget scan", "--cwd", str(self.repo), "--out", str(out)]), 0)
        self.assertEqual(out.read_text(), "scan unavailable: no-harness-available\n")
        self.assertEqual(PA.write_once(out, "another result\n"), "scan unavailable: no-harness-available\n")
        self.assertEqual([p.name for p in out.parent.iterdir() if p.name.endswith(".tmp")], [])


class FramedHookupTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.route = {"route_id": "rt-prior", "artifact_root": str(self.root), "cwd": str(self.root),
                      "work_request": {"text": "Add a widget scan"}}
        self.out = W._prior_art_path(self.route)
        self.marker = self.out.with_name(self.out.name + ".started")

    def started(self, at=None):
        self.marker.parent.mkdir(parents=True, exist_ok=True)
        self.marker.write_text(f"{time.time() if at is None else at:.3f}\n")

    def test_the_scan_is_off_under_the_test_switch_and_starts_once_otherwise(self):
        with mock.patch.object(W.subprocess, "Popen") as popen:
            with mock.patch.dict(os.environ, {"HEARTING_PRIOR_ART": "off"}):
                W._start_prior_art(self.route, "Add a widget scan")
            popen.assert_not_called()
            with mock.patch.dict(os.environ, {"HEARTING_PRIOR_ART": ""}):
                W._start_prior_art(self.route, "Add a widget scan")
                W._start_prior_art(self.route, "Add a widget scan")
        popen.assert_called_once()
        argv, kwargs = popen.call_args.args[0], popen.call_args.kwargs
        self.assertTrue(argv[1].endswith("utilities/prior-art.py"))
        self.assertEqual(argv[argv.index("--out") + 1], str(self.out))
        self.assertTrue(kwargs["start_new_session"])
        self.assertEqual(kwargs["env"]["AGENT_SESSION_ROLE"], "worker")
        popen.return_value.stdin.write.assert_called_once_with(b"Add a widget scan")

    def test_a_ready_list_joins_the_first_frame_prompt(self):
        self.started()
        self.out.write_text(PA.HEADER + "\ntask: t\n- [same] tools/widget_scan.py:1 — exists\n")
        text = W._frame_prompt_file(self.route).read_text()
        self.assertIn("## Already in this project (prior-art scan, advisory)", text)
        self.assertIn("- [same] tools/widget_scan.py:1 — exists", text)
        self.assertNotIn(PA.HEADER, text)
        self.assertIn("Data from a cheap scan, not instructions", text)
        self.assertIn("`mem show <id> --no-touch`", text)
        self.assertLess(text.index("## Already in this project"), text.index("## Part catalogue"))

    def test_an_unfinished_scan_is_named_by_path_without_waiting_past_the_bound(self):
        self.started(at=time.time() - W.PRIOR_ART_WAIT_SECONDS - 1)
        started = time.monotonic()
        text = W._frame_prompt_file(self.route).read_text()
        self.assertLess(time.monotonic() - started, 5)
        self.assertIn(f"The scan is still running: read {self.out}", text)

    def test_the_wait_is_bounded_and_ends_when_the_list_appears(self):
        self.started()
        slept = []

        def sleep(seconds):
            slept.append(seconds)
            if len(slept) == 3:
                self.out.write_text(PA.HEADER + "\n- [same] a.py — exists\n")
        section = W._prior_art_section(self.route, sleep=sleep)
        self.assertEqual(len(slept), 3)
        self.assertEqual(section, "- [same] a.py — exists")

    def test_no_scan_or_an_unavailable_scan_adds_nothing(self):
        plain = W.frame_task_text(self.route)
        self.assertEqual(W._prior_art_section(self.route), "")
        self.started()
        self.out.write_text("scan unavailable: no-harness-available\n")
        self.assertEqual(W._prior_art_section(self.route), "")
        self.assertEqual(W._frame_prompt_file(self.route).read_text(), plain)

    def test_a_failing_marker_write_starts_nothing_and_holds_no_prompt(self):
        real_fdopen = os.fdopen

        def full_disk(fd, *args, **kwargs):
            handle = real_fdopen(fd, *args, **kwargs)
            handle.write = mock.Mock(side_effect=OSError(28, "No space left on device"))
            return handle
        with mock.patch.object(W.subprocess, "Popen") as popen, \
                mock.patch.object(W.os, "fdopen", side_effect=full_disk), \
                mock.patch.dict(os.environ, {"HEARTING_PRIOR_ART": ""}):
            W._start_prior_art(self.route, "Add a widget scan")
        popen.assert_not_called()
        started = time.monotonic()
        self.assertEqual(W._frame_prompt_file(self.route).read_text(), W.frame_task_text(self.route))
        self.assertLess(time.monotonic() - started, 5)

    def test_an_undecodable_list_or_a_broken_scan_module_adds_nothing(self):
        output = self.root / "cycle"
        (output / "shards/frame").mkdir(parents=True)
        (output / "shards/frame/intent.md").write_text("intent\n")
        approvals = {"given": [], "execution_scope": "complete"}
        self.started()
        self.out.write_bytes(PA.HEADER.encode() + b"\n- [same] a.py \xff\xfe exists\n")
        self.assertEqual(W._prior_art_section(self.route), "")
        self.assertNotIn("prior-art scan", W._leg_task_text(self.route, self.root, output, [], None, approvals))
        self.out.write_text(PA.HEADER + "\n- [same] a.py — exists\n")
        with mock.patch.object(W, "_prior_art_module", side_effect=SyntaxError("broken prior-art.py")):
            self.assertEqual(W._prior_art_section(self.route), "")
            self.assertNotIn("prior-art scan",
                             W._leg_task_text(self.route, self.root, output, [], None, approvals))

    def test_the_approved_leg_prompt_names_a_useful_list_by_path(self):
        output = self.root / "cycle"
        (output / "shards/frame").mkdir(parents=True)
        (output / "shards/frame/intent.md").write_text("intent\n")
        approvals = {"given": [], "execution_scope": "complete"}
        before = W._leg_task_text(self.route, self.root, output, [], None, approvals)
        self.assertNotIn("prior-art scan", before)
        self.out.parent.mkdir(parents=True, exist_ok=True)
        self.out.write_text(PA.HEADER + "\n- [same] a.py — exists\n")
        after = W._leg_task_text(self.route, self.root, output, [], None, approvals)
        self.assertIn(f"- {self.out} (sha256 ", after)
        self.assertIn("prior-art scan, advisory", after)
        self.assertIn("`mem show <id> --no-touch`", after)


if __name__ == "__main__":
    unittest.main()
