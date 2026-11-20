"""`hearting update` and `harness config status` show what the model-follow survey found.

The survey itself is `utilities/model_follow.py`; here only the surfaces are checked, with a
stand-in survey: update carries its lines on every path (new release, up to date, pinned
timer run), a survey that fails never fails the update, and the models.conf status reading
gets "newer n · proposal n" only when a count is not zero.
"""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import importlib.util
import io
import json
import os
import subprocess
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "utilities"))
import distribution  # noqa: E402
import fixture_env  # noqa: E402
import installer  # noqa: E402
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
        # Only the fixture's own roots and an empty PATH (no harness CLI is ever run).
        self.env = fixture_env.build_environment(self.root, ROOT, base={"PATH": ""})
        self.env["OPENCODE_RUNTIME_VERSION"] = "1.19.0"
        home, config, cache = (Path(self.env[key]) for key in ("HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME"))
        homes = {"claude": Path(self.env["CLAUDE_CONFIG_DIR"]), "codex": Path(self.env["CODEX_HOME"]),
                 "opencode": config / "opencode"}
        for harness, base in homes.items():
            target = base / "agent-config" / "models.conf"
            target.parent.mkdir(parents=True)
            target.write_bytes((ROOT / "adapters" / harness / "config" / "models.conf").read_bytes())
        self.places = {"claude": homes["claude"] / "cache/model-catalog/one.json",
                       "codex": homes["codex"] / "models_cache.json",
                       "opencode": cache / "opencode/models.json"}
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
        user = Path(self.env["CODEX_HOME"]) / "agent-config" / "models.conf"
        user.write_text(user.read_text().replace("CFG_MODEL_PROFILE_DEEP=deep:xhigh",
                                                 f"CFG_MODEL_PROFILE_DEEP=model/{slug(1)}:high"))
        lines, _ = self.update_in_child()  # the same fixture with a regular list does say something
        self.assertTrue([t for t in lines if f"{slug(1)} -> {slug(2)}" in t], lines)
        for place in self.places.values():
            # destructive-ok: reason=swap this fixture's list for a pipe; boundary=file under this test's TemporaryDirectory
            place.unlink(missing_ok=True)
            os.mkfifo(place)
        lines, elapsed = self.update_in_child()
        self.assertEqual(lines, [])
        self.assertLess(elapsed, 15)


class SlowServiceCallerTests(unittest.TestCase):
    """One slow OpenCode service answer reaches every caller of the catalog read.

    The answer's headers come at once and its body in pieces: each read ends well inside the
    two seconds a read may take, the whole is longer than the three seconds a survey allows.
    Only the survey gives up; launch, Fleet and the installer read it to the end, as they did
    before the survey existed.
    """
    PIECES, PAUSE = 6, 0.6

    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="mf1s-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.env = fixture_env.build_environment(self.root, ROOT, base={"PATH": ""})
        patch = mock.patch.dict(os.environ, self.env, clear=True)
        patch.start()
        self.addCleanup(patch.stop)
        config = Path(self.env["XDG_CONFIG_HOME"]) / "opencode" / "agent-config" / "models.conf"
        config.parent.mkdir(parents=True)
        text = (ROOT / "adapters" / "opencode" / "config" / "models.conf").read_text()
        chosen = text.replace("CFG_MODEL_PROFILE_DEEP=balanced-deep:xhigh",
                              "CFG_MODEL_PROFILE_DEEP=model/p/elm-1:high")
        self.assertNotEqual(chosen, text)
        config.write_text(chosen)
        body = json.dumps({"data": [
            {"providerID": "p", "id": f"elm-{n}", "enabled": True, "status": "active",
             "family": "elm", "variants": [{"id": "high"}]} for n in (1, 2)]}).encode()
        size = -(-len(body) // self.PIECES)
        parts = [body[i:i + size] for i in range(0, len(body), size)]

        class Slow(BaseHTTPRequestHandler):
            def do_GET(handler):
                handler.send_response(200)
                handler.send_header("Content-Length", str(len(body)))
                handler.end_headers()
                handler.wfile.flush()
                try:
                    for part in parts:
                        time.sleep(self.PAUSE)
                        handler.wfile.write(part)
                        handler.wfile.flush()
                except OSError:
                    pass

            def log_message(handler, *_args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Slow)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        record = Path(self.env["XDG_STATE_HOME"]) / "opencode" / "service.json"
        record.parent.mkdir(parents=True)
        record.write_text(json.dumps({"version": "2.0.32", "url": f"http://127.0.0.1:{server.server_port}"}))
        record.chmod(0o600)
        spec = importlib.util.spec_from_file_location(
            "slow_service_wrapper", ROOT / "adapters" / "opencode" / "bin" / "dispatch-headless.py")
        self.wrapper = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.wrapper)

    def callers(self):
        from tools.fleet.explorer import model_catalog
        wrapper = self.wrapper

        def launch():
            args = SimpleNamespace(worktree=str(self.root), model_profile=None, worker_type="stage")
            with mock.patch.object(wrapper.opencode_runtime, "model_availability",
                                   return_value={"automatic_eligible": True, "state": "usable"}):
                return wrapper._fit_to_catalog(
                    args, {"model": "elm-2", "variant": "xhigh", "source": "explicit"})

        def fleet():
            catalog = model_catalog.read_catalog("opencode")
            return catalog.state, catalog.models

        def install():
            done = installer.cmd_model(SimpleNamespace(
                model_command="set", harness="opencode", target="profile/deep",
                model="p/elm-2@xhigh", dry_run=True))
            return done["status"], done["exit"]

        def survey():
            return model_follow.lines([model_follow.survey_harness("opencode")])

        return {"launch": launch, "fleet": fleet, "install": install, "survey": survey}

    def test_only_the_survey_ends_early_and_the_others_read_the_whole_answer(self):
        callers = self.callers()
        started = time.monotonic()
        with ThreadPoolExecutor(max_workers=len(callers)) as pool:
            futures = {name: pool.submit(call) for name, call in callers.items()}
            got = {name: future.result(timeout=30) for name, future in futures.items()}
        self.assertGreater(self.PIECES * self.PAUSE, 3.0)  # longer than a survey waits
        self.assertEqual(got["survey"], [], "the survey ends quietly")
        self.assertEqual(got["fleet"], ("available", ("p/elm-1", "p/elm-2")))
        self.assertEqual((got["launch"]["model"], got["launch"]["variant"]), ("p/elm-2", "high"))
        self.assertEqual((got["launch"]["model_fit_from"], got["launch"]["variant_fit_from"]),
                         ("elm-2", "xhigh"))
        self.assertEqual(got["install"], ("invalid-arguments", 64))
        self.assertLess(time.monotonic() - started, 20)

    def test_the_same_fixture_answering_promptly_gives_the_survey_its_line(self):
        self.PAUSE = 0.0
        lines = self.callers()["survey"]()
        self.assertEqual(len(lines), 1, lines)
        self.assertIn("p/elm-1 -> p/elm-2", lines[0])


if __name__ == "__main__":
    unittest.main()
