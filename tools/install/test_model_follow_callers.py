"""The real callers of the model-follow pass: update's end pass, the activation (refresh), and
the lines each says.

`utilities/model_follow_write.test.py` tests the pass itself. Here the code that calls it is
run as it is: a table another process holds locked is waited for a second at most (the
activation holds locks of its own paths, so a long wait was a deadlock with
`hearting model set <harness> context-window`), an update touches only the harnesses it
handles, what a redraw refresh did to a table is said, and a release that reads an activation
report of this one still hears of the first edit to the table.
"""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import json
import subprocess
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools" / "install"))
sys.path.insert(0, str(ROOT / "utilities"))
import distribution  # noqa: E402
import fixture_env  # noqa: E402
import model_config  # noqa: E402
import model_follow  # noqa: E402
import model_settings  # noqa: E402
import native_context  # noqa: E402
import runtime_activation  # noqa: E402
import safe_fs  # noqa: E402

HARNESSES = ("claude", "codex", "opencode")
LOCK_HOLDER = (
    "import sys\n"
    "sys.path.insert(0, sys.argv[1])\n"
    "import safe_fs\n"
    "from pathlib import Path\n"
    "with safe_fs.TargetLock(Path(sys.argv[2])):\n"
    "    print('held', flush=True)\n"
    "    sys.stdin.read()\n"
)


def slug(number):
    """A made-up Codex-shaped model name, never a real model id."""
    return f"gpt-{number}-elm"


class Fixture(unittest.TestCase):
    """A home with the three shipped tables and a Codex list of two models; nothing real is read."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="mf3c-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.env = fixture_env.build_environment(self.root, ROOT, base={"PATH": ""})
        self.env["OPENCODE_RUNTIME_VERSION"] = "1.19.0"
        patcher = mock.patch.dict(os.environ, self.env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.homes = {"claude": Path(self.env["CLAUDE_CONFIG_DIR"]), "codex": Path(self.env["CODEX_HOME"]),
                      "opencode": Path(self.env["XDG_CONFIG_HOME"]) / "opencode"}
        for harness, base in self.homes.items():
            self.table(harness).parent.mkdir(parents=True)
            self.table(harness).write_bytes((ROOT / "adapters" / harness / "config" / "models.conf").read_bytes())
        (self.homes["codex"] / "models_cache.json").write_text(json.dumps({"models": [
            {"slug": slug(1), "visibility": "list"}, {"slug": slug(2), "visibility": "list"}]}))
        self.set_cell("codex", f"CFG_MODEL_PROFILE_DEEP=model/{slug(1)}:high")

    def table(self, harness):
        return self.homes[harness] / "agent-config" / "models.conf"

    def set_cell(self, harness, line):
        key = line.split("=", 1)[0]
        path = self.table(harness)
        path.write_text("\n".join(line if l.startswith(key + "=") else l
                                  for l in path.read_text().splitlines()) + "\n")

    def baks(self, harness):
        return sorted(p.name for p in self.table(harness).parent.glob("models.conf.bak*"))

    def bounded(self, call, seconds=20.0):
        """Run `call` and fail (rather than hang) when it waits for the lock without end."""
        out = {}
        thread = threading.Thread(target=lambda: out.update(lines=call()), daemon=True)
        thread.start()
        thread.join(seconds)
        self.assertFalse(thread.is_alive(), "the caller waits for the table's lock without end")
        return out["lines"]

    @contextmanager
    def held_by_another_process(self, path):
        proc = subprocess.Popen(
            [sys.executable, "-I", "-B", "-c", LOCK_HOLDER, str(ROOT / "tools" / "install"), str(path)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, env=dict(os.environ))
        try:
            self.assertEqual(proc.stdout.readline().strip(), "held")
            yield
        finally:
            proc.stdin.close()
            proc.wait(timeout=10)


class LockWaitTests(Fixture):
    def callers(self):
        return {
            "update's end pass": lambda: distribution._model_follow_pass(["codex"])[0],
            "activation": lambda: runtime_activation._follow_models("codex", ROOT, "global"),
        }

    def test_a_table_another_process_holds_is_given_up_after_about_a_second_by_every_caller(self):
        table = self.table("codex")
        before = table.read_bytes()
        for name, call in self.callers().items():
            with self.subTest(caller=name), self.held_by_another_process(table):
                started = time.monotonic()
                lines = self.bounded(call)
                took = time.monotonic() - started
                self.assertLess(took, 5.0)
                self.assertGreaterEqual(took, 0.8)
                self.assertEqual(len(lines), 1, lines)
                self.assertTrue(lines[0].startswith("model follow: codex table not updated (lock-timeout"), lines)
                self.assertEqual((table.read_bytes(), self.baks("codex")), (before, []))
        # once it is free the same callers follow
        lines = self.callers()["activation"]()
        self.assertTrue([t for t in lines if f"{slug(1)} -> {slug(2)}" in t], lines)

    def test_a_pass_that_cannot_take_the_lock_never_fails_the_update(self):
        table = self.table("codex")
        with self.held_by_another_process(table), \
                mock.patch.object(distribution, "_update_release", return_value={"status": "up-to-date"}):
            result = self.bounded(lambda: distribution.update(runtimes=["codex"]))
        self.assertEqual(result["status"], "up-to-date")
        self.assertTrue(result["report_lines"][0].startswith("model follow: codex table not updated (lock-timeout"))

    def test_the_default_wait_is_short_and_nothing_waits_without_end(self):
        self.assertLessEqual(model_follow.LOCK_WAIT, 1.0)
        seen = []
        real = model_settings.edit_file

        def spy(*args, **kwargs):
            seen.append(kwargs.get("lock_wait"))
            return real(*args, **kwargs)
        with mock.patch.object(model_settings, "edit_file", side_effect=spy):
            model_follow.apply_harness("codex")
            distribution._model_follow_pass(["codex"])
            runtime_activation._follow_models("codex", ROOT, "global")
            model_follow.revert_rejected("codex", slug(2))
        self.assertTrue(seen)
        self.assertTrue(all(wait is not None and wait <= 1.0 for wait in seen), seen)


class OverlapWithContextWindowTests(Fixture):
    """An activation holds the lock of a native file of its own and then the pass takes the
    table's lock; `model set <harness> context-window` holds the table's lock and then takes that
    native file's lock. Both end, in either order they meet."""

    def run_pair(self, harness):
        native = self.homes[harness] / ("settings.json" if harness == "claude" else "config.toml")
        a_holds, b_inside = threading.Event(), threading.Event()
        out = {}

        def project(*_args, **_kwargs):  # what the projection does: lock the native file
            b_inside.set()
            with safe_fs.TargetLock(native):
                return {"report_lines": [], "path": str(native)}

        def refresh():
            with safe_fs.TargetLock(native):
                a_holds.set()
                out["inside"] = b_inside.wait(10)
                started = time.monotonic()
                out["lines"] = runtime_activation._follow_models(harness, ROOT, "global")
                out["waited"] = time.monotonic() - started

        def model_set():
            a_holds.wait(10)
            out["set"] = model_settings.set_model(harness, "context-window", "650000",
                                                  runtime=self.homes[harness], source_root=ROOT)

        with mock.patch.object(native_context, "project_context_window", side_effect=project):
            threads = [threading.Thread(target=refresh, daemon=True), threading.Thread(target=model_set, daemon=True)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(30)
        return out, [thread.is_alive() for thread in threads]

    def test_the_activation_and_model_set_context_window_both_end(self):
        for harness in ("claude", "codex"):
            with self.subTest(harness=harness):
                out, alive = self.run_pair(harness)
                self.assertEqual(alive, [False, False], "a deadlock: both wait for each other")
                self.assertTrue(out["inside"])
                self.assertLess(out["waited"], 5.0)
                self.assertEqual(out["set"]["status"], "changed")
                self.assertEqual(int(model_config.resolve_context_window(
                    model_config.parse_config(self.table(harness)))[0]), 650000)


class UpdateHandlesFewerHarnessesTests(Fixture):
    def setUp(self):
        super().setUp()
        rows = tuple({"id": f"a-{n}", "family": "a", "version": (n,), "visible": True, "deprecated": False,
                      "efforts": None, "cost": None, "upgrade": None, "min_cli": None} for n in (1, 2))
        catalog = SimpleNamespace(state="available", rows=rows, client_version=None)
        patcher = mock.patch.object(model_follow, "_catalog", return_value=catalog)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.set_cell("claude", "CFG_MODEL_PROFILE_DEEP=model/a-1:high")
        self.set_cell("codex", "CFG_MODEL_PROFILE_DEEP=model/a-1:high")
        self.refreshes = []

        def run(command, **kwargs):
            self.refreshes.append(command[command.index("--runtime") + 1])
            return SimpleNamespace(returncode=0, stdout="{}", stderr="")
        patcher = mock.patch.object(distribution.subprocess, "run", side_effect=run)
        patcher.start()
        self.addCleanup(patcher.stop)

    def update(self, **kwargs):
        with mock.patch.object(distribution, "_update_release",
                               return_value={"status": "up-to-date", "release_root": str(ROOT)}):
            return distribution.update(**kwargs)

    def deep(self, harness):
        return model_config.parse_config(self.table(harness))["CFG_MODEL_PROFILE_DEEP"]

    def test_only_the_chosen_runtime_is_followed_and_redrawn(self):
        result = self.update(runtimes=["claude"])
        self.assertEqual((self.deep("claude"), self.deep("codex")), ("model/a-2:high", "model/a-1:high"))
        self.assertEqual(self.refreshes, ["claude"])
        self.assertEqual(self.baks("codex"), [])
        self.assertFalse([t for t in result["report_lines"] if "codex" in t], result["report_lines"])

    def test_without_a_choice_only_the_activated_harnesses_are_followed(self):
        activated = {"codex": {"mode": "packaged"}}
        with mock.patch.object(distribution, "_activation_state", side_effect=lambda r: activated.get(r)):
            self.update()
        self.assertEqual((self.deep("claude"), self.deep("codex")), ("model/a-1:high", "model/a-2:high"))
        self.assertEqual(self.refreshes, ["codex"])
        self.assertEqual(self.baks("claude"), [])

    def test_an_uninstalled_harness_is_not_followed_and_no_refresh_is_asked_for_it(self):
        with mock.patch.object(distribution, "_activation_state", return_value=None):
            result = self.update()
        self.assertEqual((self.deep("claude"), self.deep("codex")), ("model/a-1:high", "model/a-1:high"))
        self.assertEqual((self.refreshes, result.get("report_lines")), ([], None))


class RedrawLinesTests(Fixture):
    LINE = f"model follow: codex tier/deep {slug(1)} back to {slug(0)} · {slug(1)} rejected"

    def refresh_says(self, document, returncode=0):
        done = SimpleNamespace(returncode=returncode, stdout=json.dumps(document), stderr="")
        with mock.patch.object(distribution.subprocess, "run", return_value=done):
            return distribution._redraw_agent_definitions(ROOT, ["codex"])

    def test_what_the_redraw_refresh_did_to_the_table_is_said(self):
        self.assertEqual(self.refresh_says({"runtime": "codex", "model_follow_lines": [self.LINE],
                                            "lines": ["codex: x", self.LINE]}), [self.LINE])

    def test_a_failed_refresh_says_both_what_it_did_and_how_to_redraw(self):
        notes = self.refresh_says({"command": "refresh", "runtimes": [], "lines": [self.LINE]}, returncode=1)
        self.assertEqual(notes[0], self.LINE)
        self.assertIn("run `harness runtime refresh --runtime codex`", notes[1])

    def test_update_adds_them_once_to_its_own_lines(self):
        with mock.patch.object(distribution, "_update_release", return_value={"status": "up-to-date", "release_root": "/r"}), \
                mock.patch.object(distribution, "_model_follow_pass", return_value=([self.LINE], ["codex"])), \
                mock.patch.object(distribution.subprocess, "run", return_value=SimpleNamespace(
                    returncode=0, stdout=json.dumps({"model_follow_lines": [self.LINE]}), stderr="")):
            result = distribution.update(runtimes=["codex"])
        self.assertEqual(result["report_lines"], [self.LINE])

    def test_an_activation_that_failed_after_editing_the_table_still_tells_what_was_edited(self):
        line = f"model follow: codex tier/deep {slug(1)} -> {slug(2)} (high) · backup models.conf.bak"
        failed = SimpleNamespace(returncode=3, stderr="", stdout=json.dumps(
            {"command": "activate", "runtimes": [{"runtime": "codex"}, {"error": "boom"}],
             "lines": [line, "runtime activate: blocked: boom"]}))
        with mock.patch.object(distribution.subprocess, "run", return_value=failed):
            with self.assertRaises(distribution.DistributionError) as caught:
                distribution._activate_release(ROOT, ["codex"])
        self.assertIn("blocked: boom", str(caught.exception))
        self.assertIn(line, str(caught.exception))


class UpToDateUpdateTests(Fixture):
    """An update that finds its release current says what it did and leaves the table, and the
    definitions drawn from it, agreeing: it never follows a model and goes back in one run."""

    def test_a_released_cell_whose_model_the_record_shows_refused_is_not_followed_again(self):
        import model_follow_mark as K
        import model_outcome_record as M
        held = K.render(K.held_record("2026-11-21", slug(2), slug(1)))
        self.set_cell("codex", f"CFG_TIER_DEEP_MODEL={slug(1)} {held}")
        M.record_unavailable("codex", "codex", slug(2), "unscoped", "model-unavailable", source="registry-close")
        model_settings.set_model("codex", "tier/deep", "follow", runtime=self.homes["codex"], source_root=ROOT)
        table, baks = self.table("codex"), self.baks("codex")
        redraws = []
        done = SimpleNamespace(returncode=0, stdout="{}", stderr="")
        with mock.patch.object(distribution, "_update_release", return_value={"status": "up-to-date", "release_root": str(ROOT)}), \
                mock.patch.object(distribution.subprocess, "run", side_effect=lambda cmd, **kw: redraws.append(cmd) or done):
            result = distribution.update(runtimes=["codex"])
        said = [t for t in result.get("report_lines", []) if "tier/deep" in t]
        self.assertEqual(said, [])
        self.assertEqual(model_config.parse_config(table)["CFG_TIER_DEEP_MODEL"], slug(1))
        self.assertEqual(self.baks("codex"), baks)
        # profile/deep (a cell of its own, at the other model) also stays off the refused one
        self.assertEqual(model_config.parse_config(table)["CFG_MODEL_PROFILE_DEEP"], f"model/{slug(1)}:high")
        self.assertEqual(redraws, [])


class FirstEditIsSaidTests(Fixture):
    """The update that first edits a person's table says so, also to the release that installs it."""

    @staticmethod
    def earlier_release_gathering(report):
        """The line gathering of the release before this change (`_install_or_update`): it carries
        over only `preserved` and `notices` of an activation row."""
        rows = report["runtimes"] if isinstance(report.get("runtimes"), list) else [report]
        lines = [f"preserved user-changed: {path}" for row in rows for path in row.get("preserved", [])]
        for row in rows:
            for notice in row.get("notices", []):
                line = f"{row.get('runtime')}: {notice}"
                if isinstance(notice, str) and line not in lines:
                    lines.append(line)
        return lines

    def activate(self):
        env = {**self.env, "AGENT_HOME": str(ROOT), "PATH": "/usr/bin:/bin"}
        done = subprocess.run(
            ["/bin/sh", str(ROOT / "tools" / "install" / "harness.sh"), "runtime", "activate", "--runtime", "codex",
             "--mode", "linked", "--source", str(ROOT), "--json"],
            env=env, capture_output=True, text=True, timeout=300)
        self.assertEqual(done.returncode, 0, done.stdout[-600:] + done.stderr[-600:])
        return json.loads(done.stdout)

    def test_the_line_is_in_the_notices_an_earlier_release_reads_and_said_once_by_this_one(self):
        report = self.activate()
        wanted = [t for t in report["model_follow_lines"] if f"{slug(1)} -> {slug(2)}" in t]
        self.assertEqual(len(wanted), 1, report["model_follow_lines"])
        self.assertEqual(model_config.parse_config(self.table("codex"))["CFG_MODEL_PROFILE_DEEP"],
                         f"model/{slug(2)}:high")
        # the release that was installed before this one gathers its lines from `notices`
        earlier = self.earlier_release_gathering(report)
        self.assertIn(f"codex: {wanted[0]}", earlier)
        # this release says the same line once, and the refresh text prints it once too
        _preserved, mine = distribution._activation_report_lines(report)
        self.assertEqual([t for t in mine if "model follow:" in t].count(wanted[0]), 1)
        self.assertFalse([t for t in report["report_lines"] if "model follow:" in t])
        self.assertEqual(report["lines"].count(wanted[0]), 1)


class ReadOnlyTableThroughActivationTests(Fixture):
    def test_an_activation_leaves_a_read_only_table_and_says_so(self):
        table = self.table("codex")
        table.chmod(0o444)
        self.addCleanup(table.chmod, 0o644)
        before = table.read_bytes()
        lines = runtime_activation._follow_models("codex", ROOT, "global")
        self.assertEqual(lines, ["model follow: codex table is read-only · not updated"])
        self.assertEqual((table.read_bytes(), self.baks("codex")), (before, []))


class AnotherListThroughCallersTests(Fixture):
    """A list another CLI version wrote is not followed: every caller says so, once, and the table stays."""

    LINE = ("model follow: codex: the model list was written by CLI 0.163.0, installed is 0.171.0 "
            "· start codex once to refresh it · nothing followed")

    def setUp(self):
        super().setUp()
        (self.homes["codex"] / "models_cache.json").write_text(json.dumps({"client_version": "0.163.0", "models": [
            {"slug": slug(1), "visibility": "list"}, {"slug": slug(2), "visibility": "list"}]}))
        bin_dir = self.root / "fakebin"
        bin_dir.mkdir()
        cli = bin_dir / "codex"   # `debug models` fails here: the list is not refreshed
        cli.write_text(f'#!/bin/sh\n[ "$1" = debug ] && {{ echo x >> "{self.root / "refreshed"}"; exit 1; }}\n'
                       'echo "codex-cli 0.171.0"\n')
        cli.chmod(0o755)
        patcher = mock.patch.dict(os.environ, {"PATH": f"{bin_dir}:/usr/bin:/bin"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_update_pass_and_the_activation_say_it_and_leave_the_table(self):
        table = self.table("codex")
        before = (table.read_bytes(), table.stat().st_mtime_ns)
        for name, call in {"update's end pass": lambda: distribution._model_follow_pass(["codex"])[0],
                           "activation": lambda: runtime_activation._follow_models("codex", ROOT, "global")}.items():
            with self.subTest(caller=name):
                self.assertEqual(self.bounded(call), [self.LINE])
                self.assertEqual((table.read_bytes(), table.stat().st_mtime_ns, self.baks("codex")), (*before, []))
        self.assertEqual((self.root / "refreshed").read_text().split(), ["x", "x"])   # once per pass

    def test_the_update_report_carries_the_line(self):
        with mock.patch.object(distribution, "_update_release", return_value={"status": "up-to-date"}):
            result = self.bounded(lambda: distribution.update(runtimes=["codex"]))
        self.assertEqual([t for t in result["report_lines"] if "model follow:" in t], [self.LINE])


class OneRefreshPerUpdateTests(AnotherListThroughCallersTests):
    """A CLI that cannot write its list again is asked once per update, not once per pass: the
    activation, update's own pass and the redraw each read the list, and each used to ask."""

    def update_with_an_activation(self):
        """`update` whose release step runs the activation's follow pass the way the child does
        (the child gets this process's environment)."""
        def release(*_args, **_kw):
            runtime_activation._follow_models("codex", ROOT, "global")
            return {"status": "up-to-date"}
        with mock.patch.object(distribution, "_update_release", side_effect=release):
            return self.bounded(lambda: distribution.update(runtimes=["codex"]))

    def asked(self):
        path = self.root / "refreshed"
        return len(path.read_text().split()) if path.exists() else 0

    def test_before_each_pass_asked_again(self):  # the old behaviour, by leaving the new step out
        with mock.patch.object(model_follow, "refresh_lists_once", return_value={}), \
                mock.patch.object(model_follow, "list_refresh_done", return_value={"before": None}):
            self.update_with_an_activation()
        self.assertEqual(self.asked(), 2)  # the activation's pass and update's own

    def test_an_update_asks_the_cli_once_for_all_of_its_passes(self):
        result = self.update_with_an_activation()
        self.assertEqual(self.asked(), 1)
        self.assertEqual([t for t in result["report_lines"] if "model follow:" in t], [self.LINE])

    def test_the_environment_is_put_back_after_the_update(self):
        self.assertNotIn(model_follow.REFRESHED_ENV, os.environ)
        self.update_with_an_activation()
        self.assertNotIn(model_follow.REFRESHED_ENV, os.environ)
        self.bounded(lambda: distribution._model_follow_pass(["codex"])[0])
        self.assertEqual(self.asked(), 2)  # outside an update a pass still asks for its own

    def test_a_child_that_inherits_the_value_does_not_ask(self):
        with mock.patch.dict(os.environ, {model_follow.REFRESHED_ENV: f"{os.getpid()}:codex"}):
            self.assertEqual(self.bounded(lambda: runtime_activation._follow_models("codex", ROOT, "global")),
                             [self.LINE])
        self.assertEqual(self.asked(), 0)


    def dead_pid(self):
        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        proc.wait()
        return proc.pid

    def test_a_value_left_by_an_update_that_has_ended_is_no_value(self):
        other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        self.addCleanup(other.wait)
        self.addCleanup(other.kill)
        for name, value in {"dead pid": f"{self.dead_pid()}:codex", "no pid": "codex", "pid not a number": "x:codex",
                            "empty": "", "another live process": f"{other.pid}:codex@0.163.0",
                            "pid too large": "99999999999999999999:codex@0.163.0"}.items():
            with self.subTest(value=name), mock.patch.dict(os.environ, {model_follow.REFRESHED_ENV: value}):
                before = self.asked()
                self.update_with_an_activation()
                self.assertEqual(self.asked() - before, 1)  # this update asked once for itself
        with mock.patch.dict(os.environ, {model_follow.REFRESHED_ENV: f"{self.dead_pid()}:codex"}):
            before = self.asked()
            self.bounded(lambda: runtime_activation._follow_models("codex", ROOT, "global"))
            self.assertEqual(self.asked() - before, 1)  # a pass that no live update stands behind asks as ever

    def test_a_child_of_the_update_believes_the_value_and_a_grandchild_does_not(self):
        code = ("import sys; sys.path.insert(0, sys.argv[1]); import model_follow; "
                "print(sorted(model_follow._shared()))")
        grand = ("import subprocess, sys; print(subprocess.run([sys.executable, '-I', '-c', sys.argv[1], sys.argv[2]], "
                 "capture_output=True, text=True).stdout.strip())")
        env = {**os.environ, model_follow.REFRESHED_ENV: f"{os.getpid()}:codex@0.171.0"}
        run = lambda *argv: subprocess.run([sys.executable, "-I", *argv], env=env, capture_output=True,  # noqa: E731
                                           text=True, timeout=60).stdout.strip()
        self.assertEqual(run("-c", code, str(ROOT / "utilities")), "['codex']")        # parent = this process
        self.assertEqual(run("-c", grand, code, str(ROOT / "utilities")), "[]")         # a level deeper: asks as before

    def test_the_value_does_not_carry_an_earlier_one_and_names_only_the_judged_harnesses(self):
        inside = []
        with mock.patch.dict(os.environ, {model_follow.REFRESHED_ENV: f"{os.getpid()}:claude@1.2.3,codex@9.9"}), \
                mock.patch.object(distribution, "_update_release",
                                  side_effect=lambda *a, **k: inside.append(os.environ[model_follow.REFRESHED_ENV]) or {}):
            distribution.update(runtimes=["codex"])
        self.assertEqual(inside, [f"{os.getpid()}:codex"])   # this update's own, nothing of the earlier value
        with mock.patch.object(model_follow, "_own_home", side_effect=RuntimeError("fixture")):
            self.assertEqual(model_follow.refresh_lists_once(["codex"]), {})   # raised: not judged, not in the value
        self.assertEqual(model_follow.list_refresh_done({}), {"before": None})
        self.assertNotIn(model_follow.REFRESHED_ENV, os.environ)

    def test_the_cli_version_the_update_looked_at_is_not_asked_for_again(self):
        versions = []
        real = model_follow.installed_cli
        with mock.patch.object(model_follow, "installed_cli", side_effect=lambda h: versions.append(h) or real(h)):
            self.update_with_an_activation()
        self.assertEqual(versions, ["codex"])  # once for the pre-step, the activation's pass and update's own reuse it


class UnreadableRecordTests(Fixture):
    """A record of refused models that is there but cannot be read is not 'nothing refused'."""

    def record_path(self):
        import model_outcome_record as M
        return Path(M.state_dir()) / "codex.json"

    def write_record(self, data):
        path = self.record_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def deep(self):
        return model_config.parse_config(self.table("codex"))["CFG_MODEL_PROFILE_DEEP"]

    def test_no_record_is_nothing_refused_and_the_cell_follows(self):
        import model_outcome_record as M
        self.assertEqual(M.rejected_models("codex"), frozenset())
        result = model_follow.apply_harness("codex")
        self.assertEqual((result["status"], [e["target"] for e in result["written"]]), ("ok", ["profile/deep"]))
        self.assertEqual(self.deep(), f"model/{slug(2)}:high")

    def test_a_record_that_cannot_be_read_follows_nothing_and_says_so(self):
        import model_outcome_record as M
        for name, data in {"not json": b"{not json", "not utf-8": b"\xff\xfe\x00", "another harness":
                           json.dumps({"schema_version": M.SCHEMA_VERSION, "harness": "claude", "providers": {}}).encode(),
                           "a later version": json.dumps({"schema_version": M.SCHEMA_VERSION + 1, "harness": "codex",
                                                          "providers": {}}).encode()}.items():
            with self.subTest(record=name):
                self.write_record(data)
                with self.assertRaises(M.RecordUnreadable):
                    M.rejected_models("codex")
                before = self.table("codex").read_bytes()
                result = model_follow.apply_harness("codex")
                self.assertEqual((result["status"], result["reason"], result["written"], result["reverted"]),
                                 ("skipped", "record-unreadable", [], []))
                self.assertEqual((self.table("codex").read_bytes(), self.baks("codex")), (before, []))
                self.assertEqual(model_follow.lines([result]),
                                 ["model follow: codex not followed (the record of rejected models cannot be read: "
                                  f"{self.record_path()})"])
                view = model_follow.fleet_view(["codex"])[0]
                self.assertEqual({c["followable"] for c in view["cells"]}, {None})
                self.assertEqual([c["next"] for c in view["cells"]], [None] * len(view["cells"]))

    def test_opencode_is_read_the_same_way_its_usage_cache_being_the_record(self):
        sys.path.insert(0, str(ROOT / "tools"))
        from fleet.collectors import usage_cache
        import model_outcome_record as M
        path = Path(usage_cache._path("opencode"))
        self.assertEqual(M.rejected_models("opencode"), frozenset())  # no cache: nothing refused
        path.parent.mkdir(parents=True, exist_ok=True)
        before = self.table("opencode").read_bytes()
        for name, data in {"not json": "{not json", "not ours": json.dumps(
                {"schema_version": 1, "harness": "claude", "providers": {}}), "another schema": '{"schema_version": 99}',
                           "providers not a dict": '{"schema_version": 1, "harness": "opencode", "providers": []}'}.items():
            with self.subTest(record=name):
                path.write_text(data)
                with self.assertRaises(M.RecordUnreadable):
                    M.rejected_models("opencode")
                result = model_follow.apply_harness("opencode")
                self.assertEqual((result["status"], result["reason"], result["written"], result["reverted"]),
                                 ("skipped", "record-unreadable", [], []))
                self.assertEqual((self.table("opencode").read_bytes(), self.baks("opencode")), (before, []))
                self.assertEqual(model_follow.lines([result]),
                                 ["model follow: opencode not followed (the record of rejected models cannot be "
                                  f"read: {path})"])

    def test_an_opencode_cache_from_before_providers_existed_is_nothing_refused(self):
        sys.path.insert(0, str(ROOT / "tools"))
        from fleet.collectors import usage_cache
        import model_outcome_record as M
        path = Path(usage_cache._path("opencode"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"schema_version": 1, "harness": "opencode", "fetched_at": 1, "payload": {}}))
        self.assertEqual(M.rejected_models("opencode"), frozenset())
        path.write_text(json.dumps({"schema_version": 1, "harness": "opencode", "providers": None}))
        with self.assertRaises(M.RecordUnreadable):
            M.rejected_models("opencode")

    def test_the_survey_names_the_file_too(self):
        path = self.record_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"{not json")
        result = model_follow.survey_harness("codex")
        self.assertEqual(model_follow.lines([result]),
                         ["model follow: codex not followed (the record of rejected models cannot be read: "
                          f"{path})"])

    def test_a_readable_record_is_read_as_before(self):
        import model_outcome_record as M
        M.record_unavailable("codex", "codex", slug(2), "unscoped", "model-unavailable", source="registry-close")
        self.assertEqual(M.rejected_models("codex"), frozenset({slug(2)}))
        self.assertEqual(model_follow.apply_harness("codex")["written"], [])
        self.assertEqual(self.deep(), f"model/{slug(1)}:high")


if __name__ == "__main__":
    unittest.main()
