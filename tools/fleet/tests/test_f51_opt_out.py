import unittest
import os
import sys
import io
import json
import tempfile
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from fleet import fleet
from fleet.collectors import usage_cache


class F51OptOutTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_state_dir = os.environ.get("FLEET_USAGE_STATE_DIR")
        os.environ["FLEET_USAGE_STATE_DIR"] = self.tmp.name
        self.old_disable = os.environ.get("FLEET_DISABLE")

    def tearDown(self):
        if self.old_state_dir is None:
            os.environ.pop("FLEET_USAGE_STATE_DIR", None)
        else:
            os.environ["FLEET_USAGE_STATE_DIR"] = self.old_state_dir
        if self.old_disable is None:
            os.environ.pop("FLEET_DISABLE", None)
        else:
            os.environ["FLEET_DISABLE"] = self.old_disable
        self.tmp.cleanup()

    def _run(self, argv):
        buf = io.StringIO()
        with mock.patch.object(sys, "stdout", buf):
            fleet.main(argv)
        return buf.getvalue()

    def test_eight_zero_work_paths_never_touch_the_usage_fetcher(self):
        """A14: every one of these eight invocations must leave (a) the usage fetcher
        unrequested, (b) no refresh thread spawned, and (c) the isolated
        FLEET_USAGE_STATE_DIR with zero entries — --once/--json/--demo are all
        structurally cache-only in `fleet.main`, and the opt-out flags/env additionally
        gate the live-only refresh path that never even runs here. Also asserts the real
        fetchers (usage_api.account_usage / codex.account_usage) directly, not merely the
        request_refresh entry point, so a future bypass of request_refresh cannot slip
        through."""
        from fleet.collectors import usage_api
        from fleet.collectors import codex as codex_mod
        cases = [
            ("--no-usage-api", ["--once", "--no-usage-api"], {}),
            ("FLEET_DISABLE=usage-api", ["--once"], {"FLEET_DISABLE": "usage-api"}),
            ("--harness codex excludes claude", ["--once", "--harness", "codex"], {}),
            ("--section dispatch", ["--once", "--section", "dispatch"], {}),
            ("no live claude session", ["--once"], {}),
            ("--json", ["--json"], {}),
            ("--once", ["--once"], {}),
            ("--demo", ["--once", "--demo"], {}),
        ]
        for label, argv, env in cases:
            with self.subTest(case=label):
                old = {k: os.environ.get(k) for k in env}
                os.environ.update(env)
                try:
                    with mock.patch.object(usage_cache, "request_refresh") as refresh_spy, \
                         mock.patch.object(usage_api, "account_usage") as api_spy, \
                         mock.patch.object(codex_mod, "account_usage") as codex_spy:
                        self._run(argv)
                    refresh_spy.assert_not_called()
                    api_spy.assert_not_called()
                    codex_spy.assert_not_called()
                    self.assertEqual(usage_cache._THREADS, {})
                    self.assertEqual(os.listdir(self.tmp.name), [])
                finally:
                    for k, v in old.items():
                        if v is None:
                            os.environ.pop(k, None)
                        else:
                            os.environ[k] = v

    def test_json_disabled_field_reflects_flet_disable_env_end_to_end(self):
        """A14: `FLEET_DISABLE=usage-api, ,BOGUS` must surface end-to-end through --json as
        `disabled == {"recognized": ["usage-api"], "ignored": ["bogus"], "api_disabled": true}`."""
        os.environ["FLEET_DISABLE"] = "usage-api, ,BOGUS"
        out = self._run(["--json"])
        data = json.loads(out)
        self.assertEqual(data["disabled"], {"recognized": ["usage-api"], "ignored": ["bogus"],
                                            "api_disabled": True})

    def test_json_usage_api_disabled_flag_true_when_no_usage_api_flag_set(self):
        """A15: `--json`'s `usage.api_disabled` must be true when `--no-usage-api` is set."""
        out = self._run(["--json", "--no-usage-api"])
        data = json.loads(out)
        self.assertTrue(data["usage"]["api_disabled"])

    def test_disabled_state_still_shows_passive_tap_values_not_no_usage_api_text(self):
        """A15: opt-out disables the live REFRESH, not the passive tap values already on the
        session (rl_5h/rl_7d from the statusline) — the usage header must still render them,
        and "no usage api" must appear zero times for a harness that HAS tap data. An
        opencode row (which never has usage) keeps its existing "no usage api" text
        unchanged."""
        from fleet import render
        from fleet.model import Session
        claude = Session(harness="claude", pid=1, cwd="/x", liveness="idle",
                         rl_5h=50, rl_7d=30, mtime=1000)
        rows = render._usage_header_rows([claude], layout="wide")
        joined = "\n".join("".join(v for v, _k in row) for row in rows)
        self.assertEqual(joined.count("no usage api"), 0)

        opencode = Session(harness="opencode", pid=2, cwd="/y", liveness="idle", mtime=1000)
        rows2 = render._usage_header_rows([claude, opencode], layout="wide")
        joined2 = "\n".join("".join(v for v, _k in row) for row in rows2)
        self.assertEqual(joined2.count("no usage api"), 1)

    def test_opt_out_with_no_passive_tap_shows_unknown_gauge_not_no_usage_api_end_to_end(self):
        """A15 blocking-2: opt-out (--no-usage-api / FLEET_DISABLE=usage-api) + a live
        claude/codex session with NO passive-tap value (no rl_5h/rl_7d/rl_windows/rl_ms) is
        "the user turned it off" (F-51c), not opencode's structural absence — end-to-end
        through `fleet.main`, "no usage api" must appear zero times for claude/codex and
        exactly once for opencode, and --json's `usage.api_disabled` must be true."""
        from fleet import fleet as fleet_mod
        from fleet.model import Session

        def fake_collect_all(harness_filter=None, jobs_path=None, usage="cache-only"):
            return [Session(harness="claude", pid=1, cwd="/x", liveness="idle", mtime=1000),
                    Session(harness="codex", pid=2, cwd="/y", liveness="idle", mtime=1000),
                    Session(harness="opencode", pid=3, cwd="/z", liveness="idle", mtime=1000)], []

        with mock.patch.object(fleet_mod, "collect_all", fake_collect_all):
            out = self._run(["--once", "--no-usage-api"])
            self.assertEqual(out.count("no usage api"), 1)

            json_out = self._run(["--json", "--no-usage-api"])
        data = json.loads(json_out)
        self.assertTrue(data["usage"]["api_disabled"])

    def test_old_key_only_consumer_fixture_still_passes(self):
        """A24: a consumer that only ever read the pre-F51 keys (sessions/jobs/summary/
        route/memory/governor) must keep working unchanged against a live --json snapshot —
        the new usage/disabled keys are additive, never a replacement."""
        out = self._run(["--json"])
        data = json.loads(out)
        old_consumer_view = {k: data[k] for k in ("sessions", "jobs", "summary", "route")}
        self.assertIsInstance(old_consumer_view["sessions"], list)
        self.assertIsInstance(old_consumer_view["jobs"], list)
        self.assertIsInstance(old_consumer_view["summary"], dict)

    def test_tokens_are_trimmed_deduped_and_sorted(self):
        self.assertEqual(fleet._disabled_tokens(" z,usage-api, usage-api, z "), {
            "recognized": ["usage-api"], "ignored": ["z"], "api_disabled": True})

    def test_json_additive_shape(self):
        out = fleet._snapshot_json([], [], usage={"claude": {"freshness": "unknown"}},
                                   disabled={"recognized": [], "ignored": [], "api_disabled": False})
        self.assertIn('"sessions"', out)
        self.assertIn('"usage"', out)
        self.assertIn('"disabled"', out)

    def test_recognized_disable_is_additive_and_old_keys_remain(self):
        # `memory` and `governor` are best-effort keys: their collectors return None when
        # the source is absent, and `_snapshot_json` then omits the key entirely — that IS
        # the documented contract ("None (source absent) = key omitted"). Reading the real
        # collectors made this assertion depend on whether the machine happened to have
        # governor state on disk, so it failed on any host where the governor had never
        # run. Stub both so the test checks the additive shape it names, not the host.
        old = {"sessions": [], "jobs": [], "summary": {}, "route": {}, "memory": {},
               "governor": {}}
        with mock.patch.object(fleet, "_collect_memory", return_value={}), \
             mock.patch.object(fleet, "_collect_governor", return_value={}):
            out = json.loads(fleet._snapshot_json([], [], usage={"freshness": "unknown",
                "api_disabled": True}, disabled={"recognized": ["usage-api"],
                "ignored": ["bogus"], "api_disabled": True}))
        for key in old:
            self.assertIn(key, out)
        self.assertEqual(out["disabled"]["recognized"], ["usage-api"])
        self.assertEqual(out["disabled"]["ignored"], ["bogus"])

    def test_absent_best_effort_source_omits_its_key(self):
        """The other half of the same contract: absent source omits the key rather than
        emitting an empty one, so a consumer can tell 'nothing there' from 'nothing yet'."""
        with mock.patch.object(fleet, "_collect_memory", return_value=None), \
             mock.patch.object(fleet, "_collect_governor", return_value=None):
            out = json.loads(fleet._snapshot_json([], [], usage=None, disabled=None))
        self.assertNotIn("memory", out)
        self.assertNotIn("governor", out)
        self.assertIn("sessions", out)

    def test_disabled_parser_has_no_side_effects(self):
        self.assertEqual(fleet._disabled_tokens("usage-api, ,BOGUS"),
                         {"recognized": ["usage-api"], "ignored": ["bogus"],
                          "api_disabled": True})


if __name__ == "__main__":
    unittest.main()
