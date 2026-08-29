#!/usr/bin/env python3
"""Codex model/effort must describe each matched rollout, not today's config."""
import json
import os
import sys
import tempfile
import unittest

_TOOLS_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)

from fleet.collectors import codex  # noqa: E402
from fleet.model import Session  # noqa: E402


class CodexModelEffortTest(unittest.TestCase):
    FIRST = "11111111-1111-1111-1111-111111111111"
    SECOND = "22222222-2222-2222-2222-222222222222"

    def setUp(self):
        codex._CFG.update(ts=0.0, model=None, effort=None)
        codex._TURN_CONTEXT_CACHE.clear()
        codex._SUBAGENT_INDEX.clear()
        codex._TITLE_INDEX.update(stamp=None, map={})

    def _rollout(self, home, sid, contexts=(), trailing=()):
        directory = os.path.join(home, "sessions", "2026", "08", "29")
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, "rollout-2026-08-29T00-00-00-%s.jsonl" % sid)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(json.dumps({
                "type": "session_meta", "payload": {"cwd": "/repo"},
            }) + "\n")
            for model, effort in contexts:
                handle.write(json.dumps({
                    "type": "turn_context",
                    "payload": {"model": model, "effort": effort},
                }) + "\n")
            for row in trailing:
                handle.write(row)
        return path

    def test_two_sessions_keep_their_own_effort_after_global_config_changes(self):
        with tempfile.TemporaryDirectory() as home:
            with open(os.path.join(home, "config.toml"), "w", encoding="utf-8") as handle:
                handle.write('model = "gpt-current"\nmodel_reasoning_effort = "max"\n')
            first = self._rollout(home, self.FIRST, [("gpt-5.6-sol", "xhigh")])
            second = self._rollout(home, self.SECOND, [("gpt-5.6-sol", "max")])
            tick = codex._CodexTick(
                default_home=home,
                proc_paths={101: first, 102: second},
                subagents_by_home={},
            )

            old = Session(harness="codex", pid=101, cwd="/repo")
            current = Session(harness="codex", pid=102, cwd="/repo")
            codex.enrich(old, tick=tick)
            codex.enrich(current, tick=tick)

        self.assertEqual((old.model, old.effort), ("gpt-5.6-sol", "xhigh"))
        self.assertEqual((current.model, current.effort), ("gpt-5.6-sol", "max"))

    def test_latest_valid_turn_context_wins_past_a_fixed_tail_window(self):
        with tempfile.TemporaryDirectory() as home:
            path = self._rollout(
                home,
                self.FIRST,
                [("gpt-old", "high"), ("gpt-session", "xhigh")],
                trailing=[json.dumps({"type": "response_item", "payload": "z" * 131072}) + "\n"],
            )

            self.assertEqual(
                codex._rollout_model_effort(path, chunk=4096),
                ("gpt-session", "xhigh"),
            )

    def test_appended_turn_context_updates_the_cached_session_value(self):
        with tempfile.TemporaryDirectory() as home:
            path = self._rollout(home, self.FIRST, [("gpt-session", "xhigh")])
            self.assertEqual(codex._rollout_model_effort(path), ("gpt-session", "xhigh"))

            with open(path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps({
                    "type": "turn_context",
                    "payload": {"model": "gpt-session-2", "effort": "max"},
                }) + "\n")

            self.assertEqual(codex._rollout_model_effort(path), ("gpt-session-2", "max"))

    def test_config_is_fallback_when_rollout_has_no_turn_context(self):
        with tempfile.TemporaryDirectory() as home:
            with open(os.path.join(home, "config.toml"), "w", encoding="utf-8") as handle:
                handle.write('model = "gpt-fallback"\nmodel_reasoning_effort = "medium"\n')
            path = self._rollout(home, self.FIRST)
            tick = codex._CodexTick(
                default_home=home,
                proc_paths={101: path},
                subagents_by_home={},
            )
            session = Session(harness="codex", pid=101, cwd="/repo")

            codex.enrich(session, tick=tick)

        self.assertEqual((session.model, session.effort), ("gpt-fallback", "medium"))

    def test_malformed_newer_row_does_not_hide_latest_valid_context(self):
        with tempfile.TemporaryDirectory() as home:
            path = self._rollout(
                home,
                self.FIRST,
                [("gpt-session", "xhigh")],
                trailing=['{"type":"turn_context","payload":\n'],
            )

            self.assertEqual(codex._rollout_model_effort(path), ("gpt-session", "xhigh"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
