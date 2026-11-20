"""`hearting update` and `harness config status` show what the model-follow survey found.

The survey itself is `utilities/model_follow.py`; here only the surfaces are checked, with a
stand-in survey: update carries its lines on every path (new release, up to date, pinned
timer run), a survey that fails never fails the update, and the models.conf status reading
gets "newer n · proposal n" only when a count is not zero.
"""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import io
import json
import subprocess
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "utilities"))
import distribution  # noqa: E402
import model_follow  # noqa: E402
import user_config  # noqa: E402

LINE = "model follow: newer codex profile/deep model-1 -> model-2 (high) · hearting model set codex profile/deep model-2@high"


class UpdateSurfaceTests(unittest.TestCase):
    def update(self, release, lines=(LINE,), **kwargs):
        with mock.patch.object(distribution, "_update_release", return_value=release), \
                mock.patch.object(model_follow, "follow_lines", return_value=list(lines)):
            return distribution.update(**kwargs)

    def test_lines_follow_a_new_release_an_up_to_date_run_and_the_automatic_run(self):
        for status in ("updated", "up-to-date", "pinned"):
            with self.subTest(status=status):
                result = self.update({"status": status, "version": "1.0.0", "runtimes": []},
                                     automatic=status == "pinned")
                self.assertEqual(result["report_lines"], [LINE])

    def test_existing_report_lines_stay_first_and_nothing_to_say_adds_nothing(self):
        result = self.update({"status": "updated", "report_lines": ["preserved user-changed: x"]})
        self.assertEqual(result["report_lines"], ["preserved user-changed: x", LINE])
        quiet = self.update({"status": "up-to-date"}, lines=())
        self.assertNotIn("report_lines", quiet)

    def test_a_failing_survey_never_fails_the_update(self):
        with mock.patch.object(distribution, "_update_release", return_value={"status": "up-to-date"}), \
                mock.patch.object(model_follow, "follow_lines", side_effect=RuntimeError("fixture")):
            self.assertEqual(distribution.update(), {"status": "up-to-date"})

    def test_the_pinned_automatic_run_is_covered_through_the_real_release_step(self):
        state = {"channel": "pinned", "version": "1.0.0", "release_root": "/r", "archive_sha256": "x"}
        with mock.patch.object(distribution, "_load_state", return_value=state), \
                mock.patch.object(distribution, "_refresh_update_timer", side_effect=lambda result: result), \
                mock.patch.object(model_follow, "follow_lines", return_value=[LINE]):
            result = distribution.update(automatic=True)
        self.assertEqual((result["status"], result["report_lines"]), ("pinned", [LINE]))

    def test_printed_update_output_shows_the_line(self):
        out = io.StringIO()
        with redirect_stdout(out):
            distribution._print_result({"status": "up-to-date", "version": "1.0.0",
                                        "report_lines": [LINE]}, False)
        self.assertIn(LINE, out.getvalue())


class ConfigStatusTests(unittest.TestCase):
    @staticmethod
    def rows():
        return [{"id": "models-conf.codex", "status": "valid", "reading": "ok"},
                {"id": "models-conf.claude", "status": "valid", "reading": "ok"},
                {"id": "models-conf.opencode", "status": "shipped-default", "reading": "shipped default in use"},
                {"id": "fleet", "status": "valid", "reading": "ok"}]

    def test_counts_are_appended_only_when_not_zero(self):
        entries = {"codex": ["follow", "follow", "proposal", "none"], "claude": ["none"],
                   "opencode": ["proposal"]}

        def survey(runtime):
            return {"entries": [{"kind": kind} for kind in entries[runtime]]}

        with mock.patch.object(model_follow, "survey_harness", side_effect=survey):
            rows = user_config.with_model_follow(self.rows())
        self.assertEqual([row["reading"] for row in rows],
                         ["ok · newer 2 · proposal 1", "ok", "shipped default in use", "ok"])
        with mock.patch.object(model_follow, "survey_harness",
                               side_effect=lambda runtime: {"entries": [{"kind": "follow"}]}):
            only_newer = user_config.with_model_follow(self.rows())
        self.assertEqual(only_newer[0]["reading"], "ok · newer 1")

    def test_a_failing_survey_leaves_the_reading_alone(self):
        with mock.patch.object(model_follow, "survey_harness", side_effect=RuntimeError("fixture")):
            rows = user_config.with_model_follow(self.rows())
        self.assertEqual(rows[0]["reading"], "ok")


ROOT = Path(__file__).resolve().parents[2]
CHILD = """
import json, sys
from unittest import mock
for path in sys.argv[1:4]:
    sys.path.insert(0, path)
import distribution
with mock.patch.object(distribution, "_update_release", return_value={"status": "up-to-date"}):
    print(json.dumps(distribution.update().get("report_lines", [])))
"""


class ListPlaceUpdateTests(unittest.TestCase):
    """`hearting update` returns at once, with its own result, when a list place is a pipe."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="mf1u-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.env = {"HOME": str(self.root), "PATH": "", "CLAUDE_CONFIG_DIR": str(self.root / "claude"),
                    "CODEX_HOME": str(self.root / "codex"), "XDG_CONFIG_HOME": str(self.root / "config"),
                    "XDG_CACHE_HOME": str(self.root / "cache"), "XDG_DATA_HOME": str(self.root / "data"),
                    "XDG_STATE_HOME": str(self.root / "state"), "OPENCODE_RUNTIME_VERSION": "1.19.0"}
        homes = {"claude": self.root / "claude", "codex": self.root / "codex",
                 "opencode": self.root / "config" / "opencode"}
        for harness, home in homes.items():
            target = home / "agent-config" / "models.conf"
            target.parent.mkdir(parents=True)
            target.write_bytes((ROOT / "adapters" / harness / "config" / "models.conf").read_bytes())
        self.places = {"claude": self.root / "claude/cache/model-catalog/one.json",
                       "codex": self.root / "codex/models_cache.json",
                       "opencode": self.root / "cache/opencode/models.json"}
        for place in self.places.values():
            place.parent.mkdir(parents=True, exist_ok=True)

    def update_in_child(self):
        started = time.monotonic()
        done = subprocess.run(
            [sys.executable, "-I", "-B", "-c", CHILD, str(ROOT), str(ROOT / "tools" / "install"),
             str(ROOT / "utilities")], env=self.env, capture_output=True, text=True, timeout=20)
        self.assertEqual(done.returncode, 0, done.stderr)
        return json.loads(done.stdout.splitlines()[-1]), time.monotonic() - started

    def test_a_pipe_in_every_list_place_gives_the_update_result_without_waiting(self):
        slug = "gpt-{}-elm".format  # a made-up Codex-shaped name, never a real model id
        self.places["codex"].write_text(json.dumps({"models": [
            {"slug": slug(1), "visibility": "list"}, {"slug": slug(2), "visibility": "list"}]}))
        user = self.root / "codex" / "agent-config" / "models.conf"
        user.write_text(user.read_text().replace("CFG_MODEL_PROFILE_DEEP=deep:xhigh",
                                                 f"CFG_MODEL_PROFILE_DEEP=model/{slug(1)}:high"))
        lines, _ = self.update_in_child()  # the same fixture with a regular list does say something
        self.assertTrue([t for t in lines if f"{slug(1)} -> {slug(2)}" in t], lines)
        for place in self.places.values():
            place.unlink(missing_ok=True)
            os.mkfifo(place)
        lines, elapsed = self.update_in_child()
        self.assertEqual(lines, [])
        self.assertLess(elapsed, 15)


if __name__ == "__main__":
    unittest.main()
