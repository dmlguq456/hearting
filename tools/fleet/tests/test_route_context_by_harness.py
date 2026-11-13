#!/usr/bin/env python3
"""Route-row context and model colour for every harness, owner and stage worker.

Each cell writes the attempt log in the shape its runtime records (keys taken from
real attempt logs; the numbers are made up), runs that harness's own enrichment, and
reads the result where the person sees it: the shell panel's route row
(``session_line.route_doer`` -> ``doer_paint``) and Fleet's ROUTES doer
(``live._route_doer``). Before a first model response a cell has no value and the
row stays empty; that is not a fault.

The OpenCode owner cell is the one that stayed empty to the end: its supervisor kept
each step's counts out of the log. Models without a family colour (glm, deepseek)
wear the hue of the harness that runs them.
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_TOOLS_DIR = str(Path(__file__).resolve().parents[2])
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)

from fleet import render_cells, session_line, session_panel  # noqa: E402
from fleet.collectors import dispatch as dispatch_collector  # noqa: E402
from fleet.explorer import live, tables                      # noqa: E402
from fleet.model import DispatchJob, Session                 # noqa: E402
from fleet.palette import style_components                   # noqa: E402

CLAUDE_SID = "11111111-2222-3333-4444-555555555555"
CODEX_SID = "01000005-0000-7000-8000-000000000009"
OPENCODE_SID = "ses_routeContextCell"


class RouteContextByHarnessTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        self.logs = os.path.join(self.root, ".dispatch", "logs")
        os.makedirs(self.logs)
        self.cwd = os.path.join(self.root, "worktree")
        os.makedirs(self.cwd)
        env = mock.patch.dict(os.environ, {
            "AGENT_HOME": self.root, "CLAUDE_CONFIG_DIR": self.root,
            "CODEX_HOME": os.path.join(self.root, "codex-home"), "HOME": self.root,
            "XDG_STATE_HOME": os.path.join(self.root, "state"),
            "FLEET_SESSION_REGISTRY_DIR": os.path.join(self.root, "state", "agent-fleet", "sessions")})
        env.start()
        self.addCleanup(env.stop)
        for cache in (dispatch_collector._CLAUDE_STREAM_CACHE, dispatch_collector._CODEX_ATTEMPT_CACHE,
                      dispatch_collector._OPENCODE_ATTEMPT_CACHE):
            cache.clear()
            self.addCleanup(cache.clear)
        # The disk parse cache is shared state; each cell parses its own fixture.
        for name in ("load", "save"):
            patcher = mock.patch.object(dispatch_collector.parse_cache, name,
                                        return_value=None)
            patcher.start()
            self.addCleanup(patcher.stop)

    # ---- one recorded log shape per cell ---------------------------------------------
    def _job(self, harness, role, rows, model, metadata=None):
        owner = role == "owner"
        attempt = "att-%s-%s" % (harness, role)
        slug = "cell-" + ("owner" if owner else "execute")
        job = DispatchJob(key="code", slug=slug, pid=91, proc_start="w", harness=harness,
                          attempt_id=attempt, is_child=True, liveness="working")
        job.depth = job.dispatch_depth = 1 if owner else 2
        job.worker_type = "owner" if owner else "stage"
        job.route_node = "one-shot" if owner else "execute"
        job.model = model
        job.effort = "max" if harness == "opencode" else "high"
        job.cwd = self.cwd
        job._registry_metadata = dict(metadata or {})
        job._log_file = os.path.join(self.logs, "%s.%s.%s.jsonl" % (slug, attempt, harness))
        Path(job._log_file).write_text("".join(json.dumps(row) + "\n" for row in rows))
        return job

    def _claude(self, role, active):
        """Owner: the supervisor's receipt names the session; its transcript has the usage.
        Stage worker: the ``claude -p`` stream is the log."""
        usage = [] if active is None else [
            {"type": "assistant", "session_id": CLAUDE_SID, "message": {
                "model": "claude-fable-5", "usage": {"input_tokens": active,
                                                     "cache_creation_input_tokens": 0,
                                                     "cache_read_input_tokens": 0}}},
            {"type": "result", "session_id": CLAUDE_SID,
             "modelUsage": {"claude-fable-5": {"contextWindow": 1_000_000}}}]
        init = {"type": "system", "subtype": "init", "session_id": CLAUDE_SID,
                "claude_code_version": "2.1.299", "model": "claude-fable-5"}
        if role == "worker":
            return self._job("claude", role, [init] + usage, "opus")
        transcript = [init] + [dict({key: value for key, value in row.items() if key != "session_id"},
                                    sessionId=CLAUDE_SID) for row in usage]
        encoded = "".join("-" if ch in "/._" else ch for ch in self.cwd)
        project = Path(self.root, "projects", encoded)
        project.mkdir(parents=True, exist_ok=True)
        (project / (CLAUDE_SID + ".jsonl")).write_text(
            "".join(json.dumps(row) + "\n" for row in transcript))
        return self._job("claude", role, [
            {"type": "dispatch.supervisor.session", "parent_attempt_id": "att-parent",
             "session_id": CLAUDE_SID, "cwd": self.cwd}], "opus")

    def _codex(self, role, active):
        """Owner and stage worker both run under the App Server supervisor."""
        rows = [{"type": "dispatch.supervisor.turn.started", "thread_id": CODEX_SID, "turn_id": "turn-1"}]
        if active is not None:
            rows.append({"type": "dispatch.supervisor.token_usage", "thread_id": CODEX_SID,
                         "turn_id": "turn-1", "timestamp": "2026-11-13T00:00:00Z",
                         "token_usage": {"model_context_window": 258_400,
                                         "last": {"total_tokens": active},
                                         "total": {"total_tokens": active * 2}}})
        return self._job("codex", role, rows, "gpt-6.1-sol")

    def _opencode(self, role, active):
        """Owner: the session supervisor's own log (no native stream rows). Stage worker:
        the native ``opencode run --format json`` stream."""
        window = {"opencode_context_window_final_limit_context": "1000000"}
        if role == "owner":
            rows = [{"type": "dispatch.supervisor.session", "runtime": "opencode",
                     "parent_attempt_id": "att-opencode-owner", "session_id": OPENCODE_SID,
                     "cwd": self.cwd},
                    {"type": "tool_use", "timestamp": 1, "sessionID": OPENCODE_SID,
                     "part": {"type": "tool", "tool": "read", "callID": None, "id": "p1",
                              "state": {"status": "completed"}}}]
            if active is not None:
                rows.append({"type": "dispatch.supervisor.step_tokens", "runtime": "opencode",
                             "sessionID": OPENCODE_SID,
                             "tokens": {"input": active - 500, "output": 500, "reasoning": 0,
                                        "cache": {"read": 0, "write": 0}}})
            return self._job("opencode", role, rows, "opencode-go/deepseek-v4.1-flash", window)
        rows = [{"type": "step_start", "sessionID": OPENCODE_SID, "part": {"type": "step-start"}},
                {"type": "tool_use", "sessionID": OPENCODE_SID,
                 "part": {"type": "tool", "tool": "read", "state": {"status": "completed"}}}]
        if active is not None:
            rows.append({"type": "step_finish", "sessionID": OPENCODE_SID,
                         "part": {"type": "step-finish", "reason": "tool-calls",
                                  "tokens": {"input": active - 500, "output": 500, "reasoning": 0,
                                             "cache": {"read": 0, "write": 0}}}})
        return self._job("opencode", role, rows, "opencode-go/glm-5.3-flash", window)

    # ---- the readers the person looks at -----------------------------------------------
    def _collect(self, job):
        enrich = {"claude": dispatch_collector._enrich_claude_stream_session,
                  "codex": dispatch_collector._enrich_codex_attempt_session,
                  "opencode": dispatch_collector._enrich_opencode_attempt_session}[job.harness]
        enrich(job)

    def _shell_row(self, job):
        """The shell panel route row's values and paint for this job as the doer."""
        doer = session_line.route_doer({"worker_stage": job.route_node}, [job], live._get, live)
        values = session_line._doer_values(doer)
        return values, session_panel.doer_paint(values)

    CELLS = {
        # harness: (builder, active tokens, expected percent, family key or harness key)
        "claude": ("_claude", 250_000, 25, "fam_fable"),
        "codex": ("_codex", 128_200, 47, "fam_sol"),
        "opencode": ("_opencode", 250_000, 25, "hb_opencode"),
    }

    def test_every_cell_with_a_reading_carries_its_context_and_model_colour(self):
        for harness, (builder, active, percent, key) in self.CELLS.items():
            for role in ("owner", "worker"):
                with self.subTest(harness=harness, role=role):
                    job = getattr(self, builder)(role, active)
                    self._collect(job)
                    values, paint = self._shell_row(job)
                    # Shell panel route row.
                    self.assertEqual(values.get("ctx_pct"), percent)
                    self.assertNotIn("ctx_tokens", values)
                    self.assertEqual(paint["ctx"], "%d%%" % percent)
                    self.assertEqual(paint["model_key"], key)
                    self.assertEqual(paint["harness_key"], "hb_" + harness)
                    # Fleet's ROUTES doer: same reading, same colour key.
                    owner = job if role == "owner" else None
                    worker = None if role == "owner" else job
                    doer = live._route_doer(worker, owner, False)
                    self.assertEqual(doer["context"], percent)
                    self.assertEqual(doer["model"][1], key)

    def test_a_cell_before_its_first_model_response_stays_empty(self):
        for harness, (builder, _active, _percent, key) in self.CELLS.items():
            for role in ("owner", "worker"):
                with self.subTest(harness=harness, role=role):
                    job = getattr(self, builder)(role, None)
                    self._collect(job)
                    values, paint = self._shell_row(job)
                    self.assertNotIn("ctx_pct", values)
                    self.assertNotIn("ctx_tokens", values)
                    self.assertNotIn("ctx", paint)
                    # The model colour does not wait for a reading.
                    self.assertEqual(paint["model_key"], key)
                    doer = live._route_doer(None if role == "owner" else job,
                                            job if role == "owner" else None, False)
                    self.assertIsNone(doer["context"])
                    self.assertIsNone(doer["tokens"])

    def test_an_opencode_owner_with_no_known_window_shows_its_token_count(self):
        job = self._opencode("owner", 250_000)
        job._registry_metadata = {}
        self._collect(job)
        values, paint = self._shell_row(job)
        self.assertNotIn("ctx_pct", values)          # never a made-up percent
        self.assertEqual(values["ctx_tokens"], 250_000)
        self.assertEqual(paint["ctx_key"], "dim")
        self.assertEqual(live._route_doer(None, job, False)["tokens"], 250_000)

    def test_the_newest_step_is_the_owner_reading(self):
        job = self._opencode("owner", 40_000)
        with open(job._log_file, "a", encoding="utf-8") as stream:
            stream.write(json.dumps({"type": "dispatch.supervisor.step_tokens", "runtime": "opencode",
                                     "sessionID": OPENCODE_SID, "tokens": {"total": 300_000}}) + "\n")
        self._collect(job)
        self.assertEqual(job.active_context_tokens, 300_000)
        self.assertEqual(self._shell_row(job)[0]["ctx_pct"], 30)

    def test_a_step_row_from_another_session_is_refused(self):
        job = self._opencode("owner", 40_000)
        with open(job._log_file, "a", encoding="utf-8") as stream:
            stream.write(json.dumps({"type": "dispatch.supervisor.step_tokens", "runtime": "opencode",
                                     "sessionID": "ses_other", "tokens": {"total": 300_000}}) + "\n")
        self._collect(job)
        self.assertEqual(job.association_ambiguity, "multiple-stream-session-ids")
        self.assertIsNone(job.active_context_tokens)


class ModelColourRuleTest(unittest.TestCase):
    def test_a_model_without_a_family_wears_its_harness_hue(self):
        self.assertEqual(render_cells._model_key("deepseek-v4.1-flash", harness="opencode"), "hb_opencode")
        self.assertEqual(render_cells._model_key("glm-5.3-flash", dim=True, harness="opencode"), "h_opencode")
        self.assertEqual(render_cells._model_key("codex-mini", harness="codex"), "hb_codex")
        # A family keeps its own colour under any harness.
        self.assertEqual(render_cells._model_key("Opus 4.8", harness="opencode"), "fam_opus")
        self.assertEqual(render_cells._model_key("gpt-6.1-sol", dim=True, harness="codex"), "famd_sol")
        # No harness, an unknown harness, or no model name: the neutral key as before.
        self.assertEqual(render_cells._model_key("deepseek-v4.1-flash"), "fam_other")
        self.assertEqual(render_cells._model_key("deepseek-v4.1-flash", harness="other"), "fam_other")
        self.assertEqual(render_cells._model_key(None, harness="opencode"), "fam_other")
        self.assertEqual(render_cells._model_key("", harness="opencode"), "fam_other")

    def test_shell_head_and_route_row_and_fleet_rows_use_the_same_rule(self):
        # Shell panel head MODEL line (the panel's own Fleet value).
        panel = session_panel.presentation({"model": "deepseek-v4.1-flash", "effort": "max"},
                                           {}, "opencode", "ses_x", 100)
        self.assertEqual(panel["model"][0][1], "hb_opencode")
        self.assertEqual(panel["selected"][0][1], "hb_opencode")
        checking = session_panel.presentation({}, {}, "opencode", "ses_x", 100)
        self.assertEqual(checking["model"][0][1], "fam_other")      # "checking…" is not a model
        # Shell statusline model word.
        groups = session_line.line_segments({"harness": "opencode", "model": "glm-5.3-flash"}, 120)
        self.assertIn(("glm-5.3-flash", "hb_opencode"), [tuple(part) for part in groups])
        # Fleet session row (explorer) and curses harness cell.
        session = Session(harness="opencode", pid=1, proc_start="p", cwd="/x", session_id="s",
                          slug="s", model="deepseek-v4.1-flash", liveness="working")
        self.assertEqual(live._session_model_effort(session, "working")[0][1], "hb_opencode")
        cell = render_cells._harness_model_cell("opencode", "deepseek-v4.1-flash", "max", 40,
                                                "hb_opencode", dim=True)
        self.assertIn(("deepseek-v4.1-flash", "h_opencode"), cell)
        # A stage the route record names.
        self.assertEqual(live._stage_record({"id": "execute", "harness": "opencode",
                                             "model": "glm-5.3-flash"})["m"][1], "hb_opencode")

    def test_the_routes_worker_line_recedes_only_the_harness_word(self):
        parts = tables.model_cell_parts("opencode", ["deepseek-v4.1-flash", "hb_opencode"], "max")
        self.assertEqual(tables._model_cell_facts(parts),
                         ("opencode", ("deepseek-v4.1-flash", "hb_opencode"), "max"))

    def test_the_harness_hue_survives_light_mono_and_no_color(self):
        for key in ("hb_opencode", "h_opencode", "hb_codex", "hb_claude"):
            with self.subTest(key=key):
                bright = style_components(key, colors=256)
                basic = style_components(key, colors=8)
                plain = style_components(key, no_color=True)
                self.assertIsNotNone(bright["foreground"])
                self.assertNotEqual(bright["foreground"], -1)
                self.assertIn(basic["foreground"], range(1, 8))
                self.assertIsNone(plain["foreground"])
                self.assertFalse(plain["bold"] or plain["dim"] or plain["reverse"])
        # The neutral key it replaces had no hue at all.
        self.assertEqual(style_components("fam_other", colors=256)["foreground"], -1)


if __name__ == "__main__":
    unittest.main()
