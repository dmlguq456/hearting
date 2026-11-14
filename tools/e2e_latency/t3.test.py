#!/usr/bin/env python3
"""tools/e2e_latency/t3.py — frame picking, late cells, shim and launch plumbing.

Nothing here starts Fleet or measures time: ``analyze`` is fed fake PTY bytes and shim
records, the shim runs against a tiny stand-in ``fleet`` package, ``measure`` runs with
its launches mocked. No assertion is about a duration.
"""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("e2e_latency_t3", HERE / "t3.py")
t3 = importlib.util.module_from_spec(_spec)
sys.modules["e2e_latency_t3"] = t3
_spec.loader.exec_module(t3)

MS = 1_000_000


def paint(ms, *, loading=False, replay=False, ready=False, all_new=False, pending=(), carried=(),
          observed=None):
    row = {"k": "paint", "t_ns": ms * MS, "loading": loading, "replay": replay, "ready": ready,
           "all_new": all_new, "pending": list(pending), "carried": list(carried)}
    if observed is not None:          # a tree that records it: the basic collection is in
        row["observed"] = observed
    return row


def compose(ms, states):
    return {"k": "compose", "t_ns": ms * MS, "states": states}


def chunk(ms, text=""):
    return (ms * MS, text.encode())


class ObservedDefinitionTest(unittest.TestCase):
    """The new first_observed_s (basic in + the late-cell word on screen) against first_ready_s."""

    def run_story(self, *, mark, observed=True):
        text = "\x1b[1mTITLE · details wait\x1b[0m" if mark else "TITLE"
        chunks = [chunk(105, "loading"), chunk(2010, text), chunk(3010, "all done")]
        records = [paint(100, loading=True, pending=["basic"]),
                   paint(2000, pending=["required-details"], observed=observed),
                   paint(3000, ready=True, all_new=True, observed=observed)]
        return t3.analyze(chunks, records, 0)

    def test_an_observed_frame_with_the_word_counts_and_ready_keeps_the_old_definition(self):
        out = self.run_story(mark=True)
        self.assertEqual(out["first_observed_s"], 2.01)
        self.assertEqual(out["observed_by"], "details-wait")
        self.assertEqual(out["first_ready_s"], 3.01)
        self.assertEqual(out["details_complete_s"], 3.01)

    def test_an_observed_frame_without_the_word_does_not_count(self):
        out = self.run_story(mark=False)
        self.assertEqual(out["first_observed_s"], 3.01)      # the same as first_ready_s
        self.assertEqual(out["first_observed_s"], out["first_ready_s"])
        self.assertEqual(out["observed_by"], "ready")

    def test_a_tree_that_does_not_record_observed_gives_the_old_definition(self):
        out = self.run_story(mark=True, observed=None)
        self.assertEqual(out["first_observed_s"], 3.01)
        self.assertEqual(out["first_ready_s"], 3.01)

    def test_the_korean_word_counts_too(self):
        chunks = [chunk(105, "loading"), chunk(1010, "제목 · 세부 아직"), chunk(3010, "done")]
        records = [paint(100, loading=True), paint(1000, pending=["required-details"], observed=True),
                   paint(3000, ready=True, all_new=True, observed=True)]
        self.assertEqual(t3.analyze(chunks, records, 0)["first_observed_s"], 1.01)

    def test_a_replay_or_loading_frame_never_counts(self):
        chunks = [chunk(105, "loading"), chunk(510, "details wait"), chunk(3010, "done")]
        records = [paint(100, loading=True, observed=True), paint(500, replay=True, observed=True),
                   paint(3000, ready=True, all_new=True, observed=True)]
        self.assertEqual(t3.analyze(chunks, records, 0)["first_observed_s"], 3.01)


class AnalyzeTest(unittest.TestCase):
    def story(self):
        chunks = [chunk(105, "\x1b[2J불러오는 중 loading"), chunk(410, "\x1b[1mreplay\x1b[0m"),
                  chunk(620, "live-not-ready"), chunk(1520, "\x1b[31mdetails wait\x1b[0m 아직"),
                  chunk(3010, "all done")]
        records = [paint(100, loading=True, pending=["basic"]),
                   paint(400, replay=True, pending=["basic"]),
                   paint(600, pending=["required-details"]),
                   paint(1500, ready=True, carried=["required-details"]),
                   paint(3000, ready=True, all_new=True),
                   compose(90, {"basic": "pending"}),
                   compose(1400, {"basic": "ok", "required-details": "pending"}),
                   compose(2900, {"basic": "ok", "required-details": "ok"})]
        return chunks, records

    def test_picks_replay_first_observed_and_complete(self):
        chunks, records = self.story()
        out = t3.analyze(chunks, records, 0)
        self.assertEqual(out["first_byte_s"], 0.105)
        self.assertEqual(out["first_nonloading_s"], 0.41)
        self.assertEqual(out["replay_s"], 0.41)
        self.assertEqual(out["first_observed_s"], 1.52)
        self.assertEqual(out["details_complete_s"], 3.01)
        self.assertEqual(out["paints"], 5)

    def test_loading_and_replay_frames_are_never_observed(self):
        chunks = [chunk(50, "x"), chunk(200, "y")]
        records = [paint(10, loading=True, ready=True, all_new=True),
                   paint(150, replay=True, ready=True, all_new=True)]
        out = t3.analyze(chunks, records, 0)
        self.assertIsNone(out["first_observed_s"])
        self.assertIsNone(out["details_complete_s"])
        self.assertEqual(out["replay_s"], 0.2)

    def test_a_paint_with_no_later_bytes_is_not_visible(self):
        out = t3.analyze([chunk(100, "x")], [paint(500, ready=True)], 0)
        self.assertIsNone(out["first_observed_s"])
        self.assertEqual(out["first_byte_s"], 0.1)

    def test_t0_shifts_every_time(self):
        chunks, records = self.story()
        shifted = [(t + 7 * MS * 1000, d) for t, d in chunks]
        moved = [dict(r, t_ns=r["t_ns"] + 7 * MS * 1000) for r in records]
        a = t3.analyze(chunks, records, 0)
        b = t3.analyze(shifted, moved, 7 * MS * 1000)
        self.assertEqual(a["first_observed_s"], b["first_observed_s"])
        self.assertEqual(a["replay_s"], b["replay_s"])

    def test_late_cells_name_pending_carried_and_screen_words(self):
        chunks, records = self.story()
        late = t3.analyze(chunks, records, 0)["late_cells"]
        self.assertEqual(late["pending_at_first_nonloading"], ["basic"])
        self.assertEqual(late["pending_at_replay"], ["basic"])
        self.assertEqual(late["carried_at_observed"], ["required-details"])
        self.assertEqual(late["late_sources_s"], {"required-details": 1.38})
        self.assertEqual(late["screen_words_at_observed"], {"details wait": 1, "아직": 1})
        self.assertEqual(late["screen_words_after_observed"], {"details wait": 1, "아직": 1})

    def test_source_ready_is_the_first_non_pending_compose(self):
        chunks, records = self.story()
        ready = t3.analyze(chunks, records, 0)["source_ready_s"]
        self.assertEqual(ready, {"basic": 1.4, "required-details": 2.9})

    def test_cold_launch_without_replay(self):
        out = t3.analyze([chunk(300, "a"), chunk(900, "b")],
                         [paint(250, loading=True), paint(800, ready=True, all_new=True)], 0)
        self.assertIsNone(out["replay_s"])
        self.assertEqual(out["first_nonloading_s"], 0.9)
        self.assertEqual(out["first_observed_s"], out["details_complete_s"])

    def test_nothing_recorded_gives_none_not_a_crash(self):
        out = t3.analyze([], [], 0)
        self.assertIsNone(out["first_byte_s"])
        self.assertIsNone(out["first_observed_s"])
        self.assertEqual(out["source_ready_s"], {})


class HelperTest(unittest.TestCase):
    def test_summarize_nearest_rank_and_basis(self):
        out = t3.summarize([3, 1, 2])
        self.assertEqual((out["n"], out["median_s"], out["p95_s"], out["max_s"]), (3, 2, 3, 3))
        self.assertEqual(out["p95_basis"], "max-of-n")
        big = t3.summarize(range(1, 41))
        self.assertEqual(big["p95_s"], 38)
        self.assertNotIn("p95_basis", big)
        self.assertEqual(t3.summarize([None, 1.5])["n"], 1)
        self.assertEqual(t3.summarize([])["median_s"], None)

    def test_strip_ansi_and_screen_words(self):
        text = t3.strip_ansi(b"\x1b[38;5;2mhi\x1b[0m \x1b]0;title\x07there \xea\xb0\x80")
        self.assertEqual(text, "hi there 가")
        self.assertEqual(t3.screen_words("Details Wait — 아직 · loading"),
                         {"details wait": 1, "아직": 1, "loading": 1})
        self.assertEqual(t3.screen_words("all fine"), {})

    def test_read_records_skips_a_half_written_last_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "r.jsonl"
            path.write_text('{"k":"paint","t_ns":1}\n{"k":"comp')
            self.assertEqual(t3.read_records(path), [{"k": "paint", "t_ns": 1}])
            self.assertEqual(t3.read_records(Path(tmp) / "none"), [])


class EnvTest(unittest.TestCase):
    def test_child_env_is_cleared_and_isolated(self):
        with tempfile.TemporaryDirectory() as tmp:
            scratch = Path(tmp) / "s"
            fixture = {"env": {"HOME": tmp + "/h", "XDG_STATE_HOME": tmp + "/x",
                               "AGENT_DISPATCH_JOBS": tmp + "/jobs.log", "NOT_STR": 3}}
            bindir, shim = t3._prepare_bin(scratch)
            dirty = {"HERDR_PANE_ID": "p", "TMUX": "t", "CLAUDE_CONFIG_DIR": "/real",
                     "CODEX_HOME": "/real", "AGENT_SESSION_ID": "s", "OPENCODE_X": "1",
                     "XDG_CACHE_HOME": "/real/cache"}
            with mock.patch.dict(os.environ, dirty):
                env = t3.child_env(fixture, scratch=scratch, cache_dir=Path(tmp) / "c",
                                   marker="mk", tag="mk-t3-x", records=Path(tmp) / "r",
                                   bindir=bindir, shim=shim)
            for key in dirty:
                self.assertNotIn(key, {k for k in env if k != "XDG_CACHE_HOME"})
            self.assertEqual(env["XDG_CACHE_HOME"], str(Path(tmp) / "c"))
            self.assertEqual(env["HOME"], tmp + "/h")
            self.assertEqual(env["AGENT_DISPATCH_JOBS"], tmp + "/jobs.log")
            self.assertEqual(env["PATH"], str(bindir))
            self.assertEqual(env["PYTHONPATH"], str(shim))
            self.assertEqual((env["E2E_T3_MARKER"], env["E2E_T3_TAG"]), ("mk", "mk-t3-x"))
            self.assertNotIn("NOT_STR", env)
            self.assertTrue(all(isinstance(v, str) for v in env.values()))
            self.assertEqual(env["AGENT_HOME"], str(t3.WORKTREE))
            names = {p.name for p in Path(bindir).iterdir()}
            self.assertIn("python3", names)
            for absent in ("herdr", "tmux", "claude", "codex", "opencode"):
                self.assertNotIn(absent, names)

    def test_shim_source_compiles(self):
        compile(t3._SHIM, "sitecustomize.py", "exec")


class ShimTest(unittest.TestCase):
    """Run the real shim text against a stand-in ``fleet`` package, in a child python."""

    def test_shim_records_compose_paint_and_filters_the_process_census(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            pkg = tmp / "pkg" / "fleet"
            (pkg / "collectors").mkdir(parents=True)
            (pkg / "__init__.py").write_text("")
            (pkg / "collectors" / "__init__.py").write_text("")
            (pkg / "render.py").write_text(
                "_EXPLORER_READINESS = {'ready': False, 'pending': ('basic',)}\n"
                "def _explorer_compose(*a, **k):\n"
                "    global _EXPLORER_READINESS\n"
                "    _EXPLORER_READINESS = {'ready': True, 'all_new': False, 'pending': (),\n"
                "        'sources': {'basic': {'state': 'ok'}, 'usage:claude': {'state': 'continued'}}}\n"
                "    return 'frame'\n"
                "def _draw_explorer_frame(stdscr, frame, snapshot, *, stale=False):\n"
                "    return ('drawn', stale)\n")
            (pkg / "collectors" / "procscan.py").write_text(
                "import os\n"
                "class Rows(list):\n"
                "    def __init__(self, rows=(), *, complete=False, reasons=()):\n"
                "        super().__init__(rows); self.census_complete = complete; self.census_reasons = reasons\n"
                "def _ps_lines():\n"
                "    return Rows(['    1 init 00:01 init', '%d python 00:01 me' % os.getpid()], complete=True)\n")
            scratch = tmp / "scratch"
            bindir, shim = t3._prepare_bin(scratch)
            records = tmp / "rec.jsonl"
            code = (
                "from fleet import render\nfrom fleet.collectors import procscan\n"
                "class F: status = 'loading'\n"
                "print(render._draw_explorer_frame(None, F(), None, stale=False))\n"
                "print(render._explorer_compose())\n"
                "class G: status = ''\n"
                "print(render._draw_explorer_frame(None, G(), None, stale=True))\n"
                "rows = procscan._ps_lines()\n"
                "import os\n"
                "print(len(rows), rows.census_complete, rows[0].split()[0] == str(os.getpid()))\n")
            env = {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": os.pathsep.join([str(shim), str(tmp / "pkg")]),
                   "E2E_T3_RECORDS": str(records), "E2E_T3_MARKER": "marker-xyz-123",
                   "PYTHONDONTWRITEBYTECODE": "1"}
            out = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True,
                                 text=True, timeout=60)
            self.assertEqual(out.returncode, 0, out.stderr)
            self.assertEqual(out.stdout.splitlines()[:3], ["('drawn', False)", "frame", "('drawn', True)"])
            # pid 1 carries no marker and is dropped; this child's environ carries it and stays,
            # and the census-complete evidence survives the filter.
            self.assertEqual(out.stdout.splitlines()[3], "1 True True")
            rows = t3.read_records(records)
            self.assertEqual([r["k"] for r in rows], ["paint", "compose", "paint"])
            self.assertTrue(rows[0]["loading"] and not rows[0]["replay"])
            self.assertFalse(rows[0]["ready"])
            self.assertEqual(rows[0]["pending"], ["basic"])
            self.assertTrue(rows[2]["replay"] and rows[2]["ready"])
            self.assertEqual(rows[2]["carried"], ["usage:claude"])
            self.assertEqual(rows[1]["states"], {"basic": "ok", "usage:claude": "continued"})
            self.assertLess(rows[0]["t_ns"], rows[2]["t_ns"])

    def test_shim_census_keeps_a_marker_carrying_process(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            pkg = tmp / "pkg" / "fleet" / "collectors"
            pkg.mkdir(parents=True)
            (tmp / "pkg" / "fleet" / "__init__.py").write_text("")
            (pkg / "__init__.py").write_text("")
            (pkg / "procscan.py").write_text(
                "import os\n"
                "def _ps_lines():\n"
                "    return ['    1 init 00:01 init', '%d python 00:01 me' % os.getpid()]\n")
            bindir, shim = t3._prepare_bin(tmp / "scratch")
            env = {"PATH": os.environ.get("PATH", ""),
                   "PYTHONPATH": os.pathsep.join([str(shim), str(tmp / "pkg")]),
                   "E2E_T3_RECORDS": str(tmp / "rec"), "E2E_T3_MARKER": "marker-xyz-123",
                   "PYTHONDONTWRITEBYTECODE": "1"}
            code = ("import os\nfrom fleet.collectors import procscan\n"
                    "print([l.split()[0] == str(os.getpid()) for l in procscan._ps_lines()])\n")
            out = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=60)
            self.assertEqual(out.returncode, 0, out.stderr)
            # the child's environ carries the marker (E2E_T3_MARKER value), pid 1 does not
            self.assertEqual(out.stdout.strip(), "[True]")


class MeasureTest(unittest.TestCase):
    def test_measure_contract_with_launches_mocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            state = tmp / "xdg"
            (state / "hearting").mkdir(parents=True)
            stale = state / "hearting" / "fleet2-observation-abc.json"
            stale.write_text("{}")
            fixture = {"env": {"HOME": str(tmp / "h"), "XDG_STATE_HOME": str(state)},
                       "marker": "mk-test"}
            calls, reaped = [], []

            def fake_launch(env, *, records, timeout, linger, want_persist):
                cond = "second" if "cache-second" in env["XDG_CACHE_HOME"] else "cold"
                calls.append((cond, stale.exists()))
                stale.write_text("{}")            # a finished launch saved its last-complete model
                first = 2.0 if cond == "cold" else 0.5
                return {"first_byte_s": 0.1, "first_nonloading_s": first - 0.1 if cond == "second" else first,
                        "replay_s": 0.4 if cond == "second" else None, "first_observed_s": first,
                        "details_complete_s": first + 1, "late_cells": {}, "persisted": True}

            fake_fp = mock.Mock()
            fake_fp.reap.side_effect = reaped.append
            fake_fp.marked_processes.return_value = {}
            with mock.patch.object(t3, "launch", fake_launch), \
                    mock.patch.object(t3, "once_full_collect", return_value=(4.5, None)), \
                    mock.patch.object(t3, "_fixture_processes", return_value=fake_fp):
                out = t3.measure(fixture, reps=3, scratch=tmp / "scratch")
            self.assertEqual(out["metric"], "T3")
            self.assertEqual(set(out["events"]), {"cold-empty", "second-launch", "once-full-collect"})
            self.assertEqual(out["events"]["cold-empty"]["median_s"], 2.0)
            self.assertEqual(out["events"]["second-launch"]["median_s"], 0.5)
            self.assertEqual(out["events"]["once-full-collect"]["n"], 3)
            self.assertEqual(out["segments"]["second-launch"]["replay_s"]["n"], 3)
            self.assertEqual(out["segments"]["cold-empty"]["replay_s"]["n"], 0)
            self.assertEqual(out["not_measured"], [])
            # cold launches start with no saved model; second launches find the previous one
            self.assertEqual(calls, [("cold", False)] * 3 + [("second", True)] * 3)
            self.assertEqual(len(out["passes"]), 6)
            self.assertEqual(out["cleanup"]["leftover_processes"], [])
            self.assertTrue(all(t.startswith("mk-test-t3-") for t in reaped))
            self.assertIn("mk-test-t3-cold-empty-0", reaped)

    def test_second_launch_without_a_saved_model_is_reported_not_hidden(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            (tmp / "xdg").mkdir()
            fixture = {"env": {"HOME": str(tmp), "XDG_STATE_HOME": str(tmp / "xdg")}, "marker": "mk2"}

            def fake_launch(env, **kw):
                return {"first_observed_s": 1.0, "late_cells": {}}

            fake_fp = mock.Mock()
            fake_fp.marked_processes.return_value = {}
            with mock.patch.object(t3, "launch", fake_launch), \
                    mock.patch.object(t3, "once_full_collect", return_value=(None, "timeout after 180s")), \
                    mock.patch.object(t3, "_fixture_processes", return_value=fake_fp):
                out = t3.measure(fixture, reps=2, scratch=tmp / "s", conditions=("second-launch",))
            whats = [item["what"] for item in out["not_measured"]]
            self.assertIn("T3/second-launch/0", whats)
            self.assertIn("T3/once-full-collect", whats)

    def test_first_timeout_skips_the_rest_of_that_condition_and_says_why(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            (tmp / "xdg").mkdir()
            fixture = {"env": {"HOME": str(tmp), "XDG_STATE_HOME": str(tmp / "xdg")}, "marker": "mk3"}
            seen = []

            def fake_launch(env, *, records, timeout, linger, want_persist):
                cond = "second" if "cache-second" in env["XDG_CACHE_HOME"] else "cold"
                seen.append((cond, timeout))
                (tmp / "xdg" / "hearting").mkdir(exist_ok=True)
                (tmp / "xdg" / "hearting" / "fleet2-observation-x.json").write_text("{}")
                # the cold condition never leaves loading; the second one does
                return {"first_observed_s": None if cond == "cold" else 1.0, "late_cells": {}}

            fake_fp = mock.Mock()
            fake_fp.marked_processes.return_value = {}
            with mock.patch.object(t3, "once_full_collect", return_value=(1.0, None)), \
                    mock.patch.object(t3, "_fixture_processes", return_value=fake_fp):
                out = t3.measure(fixture, reps=3, scratch=tmp / "s", launch_fn=fake_launch)
            # one cold launch (timed out) and then all three second launches
            self.assertEqual([c for c, _ in seen], ["cold"] + ["second"] * 3)
            self.assertEqual({t for _, t in seen}, {t3.LAUNCH_TIMEOUT_S})
            self.assertEqual(len(out["passes"]), 4)
            cold = [n for n in out["not_measured"] if n["what"] == "T3/cold-empty"]
            self.assertEqual(len(cold), 1)
            self.assertIn("still loading", cold[0]["reason"])
            self.assertIn("remaining 2 launch(es)", cold[0]["reason"])
            self.assertEqual(out["events"]["cold-empty"]["n"], 0)
            self.assertEqual(out["events"]["cold-empty"]["no_observed_within_s"], t3.LAUNCH_TIMEOUT_S)
            self.assertEqual(out["events"]["second-launch"]["n"], 3)
            self.assertNotIn("no_observed_within_s", out["events"]["second-launch"])

    def test_write_sessions_lists_only_fixture_sessions_with_a_pid(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = {"sessions": [{"pid": 11, "cwd": "/w", "pid_start": "5", "harness": "claude"},
                                    {"pid": None, "cwd": "/w"}]}
            path = t3.write_sessions(fixture, tmp)
            self.assertEqual(json.loads(Path(path).read_text()),
                             [{"harness": "claude", "pid": 11, "cwd": "/w", "pid_start": "5"}])
            self.assertIsNone(t3.write_sessions({"sessions": []}, tmp))


if __name__ == "__main__":
    unittest.main()
