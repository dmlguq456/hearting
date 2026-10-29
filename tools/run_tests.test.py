#!/usr/bin/env python3
"""Self-regression for tools/run-tests.py (plan Step 2.4 / owner addendum A & C).

Runs the real script as a subprocess against synthetic fixture roots so the
tests exercise the actual CLI contract, not internals.
"""
from __future__ import annotations

import json
import contextlib
import io
import os
import signal
import re
import subprocess
import sys
import tempfile
import threading
import time
import textwrap
import unittest
from unittest import mock
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "tools" / "run-tests.py"
CLEAN_ENV = {k: v for k, v in os.environ.items() if not k.startswith((
    "AGENT_DISPATCH_", "AGENT_ROUTE_", "AGENT_REVIEW_", "AGENT_ARTIFACT_"))}
for _key in tuple(os.environ):
    if _key.startswith(("AGENT_DISPATCH_", "AGENT_ROUTE_", "AGENT_REVIEW_", "AGENT_ARTIFACT_")):
        os.environ.pop(_key, None)


def load_runner_module():
    """Import run-tests.py as a module for unit-level probe assertions."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("run_tests_under_test", RUNNER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

TODAY = date.today().isoformat()
YESTERDAY = (date.today() - timedelta(days=1)).isoformat()
TOMORROW = (date.today() + timedelta(days=1)).isoformat()


def run_runner(args: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(RUNNER)] + args,
        cwd=str(cwd) if cwd else str(ROOT),
        capture_output=True,
        text=True,
        timeout=120,
        env=CLEAN_ENV,
    )


def write_suite(root: Path, relpath: str, body: str) -> Path:
    p = root / relpath
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(body), encoding="utf-8")
    p.chmod(0o755)
    return p


def write_baseline(root: Path, rows: list[str]) -> Path:
    p = root / "test-baseline.tsv"
    header = ("suite_path\ttest_id\texpected_failure_kind\tisolation_profile\t"
              "reason\tdefect_id\treview_by\tfingerprint")
    # Fixture rows are written with the pre-fingerprint column count; pad them
    # so each caller does not have to carry a trailing tab.
    padded = [r if r.count("\t") >= 7 else r + "\t" for r in rows]
    p.write_text("\n".join(["# baseline", header] + padded) + "\n", encoding="utf-8")
    return p


def write_isolation_tsv(root: Path, rows: list[str] | None = None) -> Path:
    p = root / "test-isolation.tsv"
    header = "suite_path\tneeds\treason\tdefect_id\treview_by"
    p.write_text("\n".join(["# isolation"] + [header] + (rows or [])) + "\n", encoding="utf-8")
    return p


class RunTestsFixtureBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def run_fixture(self, baseline_rows, isolation_rows=None, extra_args=None, report_only=False):
        baseline = write_baseline(self.root, baseline_rows)
        isolation = write_isolation_tsv(self.root, isolation_rows)
        args = [
            "--root", str(self.root),
            "--baseline", str(baseline),
            "--isolation-tsv", str(isolation),
            "--isolation=isolated",
            "--jobs", "2",
            "--timeout", "5",
            "--no-leak-sweep",
        ]
        if report_only:
            args.append("--report-only")
        report_path = self.root / "report.tsv"
        args += ["--report", str(report_path)]
        if extra_args:
            args += extra_args
        result = run_runner(args)
        rows = []
        if report_path.exists():
            lines = report_path.read_text(encoding="utf-8").splitlines()
            header = None
            for line in lines:
                if not line or line.startswith("#"):
                    continue
                cols = line.split("\t")
                if header is None:
                    header = cols
                    continue
                rows.append(dict(zip(header, cols)))
        return result, rows


class TmuxSocketIsolationFixture(RunTestsFixtureBase):
    def test_isolated_suite_gets_a_private_tmux_socket_dir_and_no_client(self):
        # The suite fails unless tmux would resolve its socket inside the
        # suite's own temp root (HOME's parent) and no attached-client
        # variables leak from the caller.
        write_suite(self.root, "tmux_env.test.py", """\
            import os, sys
            from pathlib import Path
            root = Path(os.environ["HOME"]).parent
            socket_dir = Path(os.environ.get("TMUX_TMPDIR", "/tmp"))
            leaked = [k for k in ("TMUX", "TMUX_PANE") if k in os.environ]
            ok = socket_dir.is_dir() and root in socket_dir.parents and not leaked
            print(f"TMUX_TMPDIR={socket_dir} root={root} leaked={leaked}")
            sys.exit(0 if ok else 1)
        """)
        ambient = {"TMUX": "/tmp/tmux-0/default,1,0", "TMUX_PANE": "%1"}
        with mock.patch.dict(os.environ, ambient):
            result, rows = self.run_fixture([])
        verdicts = {r["verdict"] for r in rows if r["suite_path"] == "tmux_env.test.py"}
        self.assertEqual(verdicts, {"PASS"}, result.stdout + result.stderr)


class KnownFailFixture(RunTestsFixtureBase):
    def test_known_fail_passes_runner_with_exit_zero(self):
        write_suite(self.root, "known_fail.test.py", "import sys\nsys.exit(1)\n")
        result, rows = self.run_fixture(
            [f"known_fail.test.py\t-\texit-nonzero\tisolated\tflaky\tMA-TEST-001\t{TOMORROW}"]
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        verdicts = {r["verdict"] for r in rows if r["suite_path"] == "known_fail.test.py"}
        self.assertEqual(verdicts, {"KNOWN-FAIL"})


class KindMismatchFixture(RunTestsFixtureBase):
    def test_kind_mismatch_is_hard_failure(self):
        write_suite(self.root, "timeout_suite.test.py", "import time\ntime.sleep(30)\n")
        result, rows = self.run_fixture(
            [f"timeout_suite.test.py\t-\texit-nonzero\tisolated\tflaky\tMA-TEST-002\t{TOMORROW}"],
            extra_args=["--timeout", "1"],
        )
        self.assertNotEqual(result.returncode, 0)
        verdicts = {r["verdict"] for r in rows if r["suite_path"] == "timeout_suite.test.py"}
        self.assertEqual(verdicts, {"KIND-MISMATCH"})


class FailureKindClassifierTest(unittest.TestCase):
    def setUp(self):
        self.mod = load_runner_module()

    def result(self, stderr: str, *, stdout: str = "", timed_out: bool = False):
        return self.mod.SuiteResult(
            "fixture.test.py", 124 if timed_out else 1, timed_out,
            stdout, stderr, 0.1, "isolated",
        )

    def test_timeout_wins_over_reporter_text(self):
        result = self.result("AssertionError: late\nFAILED (failures=1)\n", timed_out=True)
        self.assertEqual(self.mod.classify_kind(result), "timeout")

    def test_error_only_unittest_run_is_error(self):
        result = self.result(
            "ERROR: test_exchange (module.ExchangeTest.test_exchange)\n"
            "RuntimeError: runner failed\nFAILED (errors=1)\n"
        )
        self.assertEqual(self.mod.classify_kind(result), "error")

    def test_mixed_failure_and_error_is_error(self):
        result = self.result(
            "FAIL: test_assertion (module.MixedTest.test_assertion)\n"
            "AssertionError: mismatch\n"
            "ERROR: test_runner (module.MixedTest.test_runner)\n"
            "RuntimeError: setup failed\n"
            "FAILED (failures=1, errors=1)\n"
        )
        self.assertEqual(self.mod.classify_kind(result), "error")

    def test_failed_word_without_unittest_structure_is_exit_nonzero(self):
        result = self.result("worker FAILED to acquire an optional probe\n")
        self.assertEqual(self.mod.classify_kind(result), "exit-nonzero")

    def test_assertion_only_unittest_run_is_assertion(self):
        result = self.result(
            "FAIL: test_value (module.ValueTest.test_value)\n"
            "AssertionError: values differ\nFAILED (failures=1)\n"
        )
        self.assertEqual(self.mod.classify_kind(result), "assertion")

    def test_unittest_output_on_stdout_is_classified(self):
        for text, expected in [
            ("ERROR: test_x (module.C.test_x)\nFAILED (errors=1)\n", "error"),
            ("FAIL: test_x (module.C.test_x)\nAssertionError: bad\nFAILED (failures=1)\n", "assertion"),
        ]:
            with self.subTest(expected=expected):
                self.assertEqual(self.mod.classify_kind(self.result("", stdout=text)), expected)
                self.assertEqual(self.mod.classify_kind(
                    self.result("", stdout=text, timed_out=True)), "timeout")

    def test_error_wins_across_capture_streams(self):
        error = "ERROR: test_x (module.C.test_x)\nFAILED (errors=1)\n"
        failure = "FAIL: test_y (module.C.test_y)\nAssertionError: bad\n"
        for stderr, stdout in [(error, failure), (failure, error)]:
            with self.subTest(stderr=stderr):
                self.assertEqual(self.mod.classify_kind(
                    self.result(stderr, stdout=stdout)), "error")


class MixedReporterFixture(RunTestsFixtureBase):
    def test_named_rows_keep_their_own_failure_kind(self):
        write_suite(self.root, "mixed.test.py", """
            import sys, unittest
            class C(unittest.TestCase):
                def test_assertion(self): self.assertEqual(1, 2)
                def test_error(self): raise RuntimeError('setup broke')
            unittest.main(testRunner=unittest.TextTestRunner(stream=sys.stdout))
        """)
        baseline = [
            f"mixed.test.py\tC.test_assertion\tassertion\tisolated\treal-bug: fixture\tTEST\t{TOMORROW}",
            f"mixed.test.py\tC.test_error\terror\tisolated\treal-bug: fixture\tTEST\t{TOMORROW}",
        ]
        result, rows = self.run_fixture(baseline)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual({row["test_id"]: row["kind"] for row in rows},
                         {"C.test_assertion": "assertion", "C.test_error": "error"})
        self.assertEqual({row["verdict"] for row in rows}, {"KNOWN-FAIL"})


class XPassFixture(RunTestsFixtureBase):
    def test_xpass_is_hard_failure_not_silent_shrink(self):
        write_suite(self.root, "now_passes.test.py", "import sys\nsys.exit(0)\n")
        result, rows = self.run_fixture(
            [f"now_passes.test.py\t-\texit-nonzero\tisolated\tstale\tMA-TEST-003\t{TOMORROW}"]
        )
        self.assertNotEqual(result.returncode, 0)
        verdicts = {r["verdict"] for r in rows if r["suite_path"] == "now_passes.test.py"}
        self.assertEqual(verdicts, {"XPASS"})


class UnlistedFailFixture(RunTestsFixtureBase):
    def test_unlisted_fail_is_hard_failure(self):
        write_suite(self.root, "surprise_fail.test.py", "import sys\nsys.exit(1)\n")
        result, rows = self.run_fixture([])
        self.assertNotEqual(result.returncode, 0)
        verdicts = {r["verdict"] for r in rows if r["suite_path"] == "surprise_fail.test.py"}
        self.assertEqual(verdicts, {"FAIL"})


class HardFailSummaryLineFixture(RunTestsFixtureBase):
    """P1: the CLI summary must carry enough to diagnose a hard-fail from
    logs alone -- verdict, kind, and a compact signature per entry."""

    def test_hard_fail_line_has_verdict_kind_and_signature(self):
        write_suite(self.root, "surprise_fail.test.py", "import sys\nsys.exit(1)\n")
        result, _rows = self.run_fixture([])
        self.assertNotEqual(result.returncode, 0)
        self.assertRegex(
            result.stdout,
            r"hard-fail: surprise_fail\.test\.py::- verdict=FAIL kind=exit-nonzero signature=",
        )


class XPassListingAndEnvLineFixture(RunTestsFixtureBase):
    """P1: XPASS-NONFATAL must list which suites unexpectedly passed, and the
    summary must carry a one-line execution environment fingerprint."""

    def test_xpass_listing_and_env_summary_line(self):
        write_suite(self.root, "now_passes.test.py", "import sys\nsys.exit(0)\n")
        result, _rows = self.run_fixture(
            [f"now_passes.test.py\t-\texit-nonzero\tisolated\tstale\tMA-TEST-015\t{TOMORROW}"],
            extra_args=["--xpass-nonfatal"],
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("xpass: now_passes.test.py::-", result.stdout)
        self.assertRegex(result.stdout, r"env: jobs=2 nproc=\d+ timeout=5 profile=isolated")


class StaleFixture(RunTestsFixtureBase):
    def test_stale_baseline_row_is_hard_failure(self):
        write_suite(self.root, "present.test.py", "import sys\nsys.exit(0)\n")
        result, rows = self.run_fixture(
            [f"absent.test.py\t-\texit-nonzero\tisolated\tgone\tMA-TEST-004\t{TOMORROW}"]
        )
        self.assertNotEqual(result.returncode, 0)
        verdicts = {r["verdict"] for r in rows if r["suite_path"] == "absent.test.py"}
        self.assertEqual(verdicts, {"STALE"})


class IsolatedEnvDropsTheLivePane(unittest.TestCase):
    """No suite process inherits the caller's herdr connection, in any profile."""

    def test_herdr_variables_never_reach_a_suite(self):
        runner = load_runner_module()
        ambient = {"HERDR_ENV": "1", "HERDR_PANE_ID": "wB:pN", "HERDR_SOCKET_PATH": "/tmp/h.sock"}
        with tempfile.TemporaryDirectory() as td, mock.patch.dict(os.environ, ambient):
            root = Path(td)
            envs = {
                "isolated": runner.build_isolated_env(root / "a"),
                "installed-layout": runner.build_installed_layout_env(root / "b", root / "prefix"),
                "ci-like": runner.build_ci_like_env(root / "c", ROOT),
            }
            for profile, env in envs.items():
                self.assertFalse([k for k in env if k.startswith("HERDR_")], profile)
            for key in ambient:
                self.assertIn(key, runner._EXPLICIT_UNSET_KEYS)


class ArtifactRootsAreNotTestCorpus(unittest.TestCase):
    def test_suites_under_an_artifact_root_are_not_collected(self):
        mod = load_runner_module()
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "utilities").mkdir()
            (root / "utilities" / "real.test.py").write_text("import sys\nsys.exit(0)\n", encoding="utf-8")
            for bucket in (
                ".agent_reports/_scratch/copy/utilities",
                ".claude_reports/legacy",
                ".dispatch/nested-home/plugins/vendor/tests",
            ):
                (root / bucket).mkdir(parents=True)
                (root / bucket / "stale.test.py").write_text("import sys\nsys.exit(1)\n", encoding="utf-8")
            found = [mod.suite_relpath(root, s) for s in mod.collect_suites(root)]
        self.assertEqual(found, ["utilities/real.test.py"])


class ExclusiveSuiteSchedulingFixture(unittest.TestCase):
    def test_projection_completeness_runs_after_parallel_batch_and_keeps_report_order(self):
        mod = load_runner_module()
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            serial = root / "tools/install/projection-completeness.test.sh"
            regular_a = root / "a.test.py"
            regular_b = root / "b.test.py"
            for suite in (serial, regular_a, regular_b):
                suite.parent.mkdir(parents=True, exist_ok=True)
                suite.touch()

            barrier = threading.Barrier(2)
            lock = threading.Lock()
            active_regular = 0
            regular_overlap = False
            serial_observed_parallel = None

            def fake_run_suite(suite, _root, _env, _profile, _timeout):
                nonlocal active_regular, regular_overlap, serial_observed_parallel
                rel = suite.relative_to(root).as_posix()
                if rel in mod.SERIAL_SUITES:
                    with lock:
                        serial_observed_parallel = active_regular
                    return rel
                if rel not in {"a.test.py", "b.test.py"}:
                    return rel
                with lock:
                    active_regular += 1
                    regular_overlap = regular_overlap or active_regular > 1
                barrier.wait(timeout=2)
                with lock:
                    active_regular -= 1
                return rel

            with mock.patch.object(mod, "run_suite", side_effect=fake_run_suite):
                results = mod.run_profile(
                    [serial, regular_a, regular_b], root, "isolated", 2, 5, None
                )

        self.assertTrue(regular_overlap)
        self.assertEqual(serial_observed_parallel, 0)
        self.assertEqual(
            results,
            [
                "tools/install/projection-completeness.test.sh",
                "a.test.py",
                "b.test.py",
            ],
        )

    def test_fast_parallel_attempt_is_removed_while_slow_peer_is_running(self):
        mod = load_runner_module()
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            fast = root / "fast.test.py"
            slow = root / "slow.test.py"
            fast.touch()
            slow.touch()
            fast_root = {}
            fast_ready = threading.Event()
            slow_started = threading.Event()
            fast_removed = threading.Event()
            slow_saw_fast_removed = []
            original_remove = mod._remove_exact_suite_tmp_root

            def fake_run(suite, suite_root, env, profile, _timeout):
                scratch = Path(env[mod.CHILD_STDERR_ENV]).parent
                if suite.name == "fast.test.py":
                    fast_root["path"] = scratch
                    fast_ready.set()
                    self.assertTrue(slow_started.wait(3))
                else:
                    slow_started.set()
                    self.assertTrue(fast_ready.wait(3))
                    self.assertTrue(fast_removed.wait(3))
                    slow_saw_fast_removed.append(not fast_root["path"].exists())
                return mod.SuiteResult(
                    suite.relative_to(suite_root).as_posix(), 0, False, "", "", .01, profile
                )

            def note_remove(path, identity):
                removed = original_remove(path, identity)
                if fast_root.get("path") == path and removed:
                    fast_removed.set()
                return removed

            with mock.patch.object(mod, "run_suite", side_effect=fake_run), \
                    mock.patch.object(mod, "reap_suite_tmp_processes", return_value=[]), \
                    mock.patch.object(mod, "_remove_exact_suite_tmp_root", side_effect=note_remove):
                results = mod.run_profile([fast, slow], root, "isolated", 2, 5, None)

        self.assertEqual([result.returncode for result in results], [0, 0])
        self.assertEqual(slow_saw_fast_removed, [True])


class FingerprintScopedSiblingRows(unittest.TestCase):
    """One (suite, test) key may carry several rows only when each carries a
    distinct, non-empty fingerprint; load picks the row for the current
    fingerprint, then the unscoped row, then the first (foreign) row."""

    # _read_tsv() skips the first non-comment line as the header row.
    HEADER = ("# baseline fixture\n"
              "suite_path\ttest_id\texpected_failure_kind\tisolation_profile"
              "\treason\tdefect_id\treview_by\tfingerprint\n")

    def setUp(self):
        self.mod = load_runner_module()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _write(self, rows):
        path = Path(self.tmp.name) / "baseline.tsv"
        path.write_text(self.HEADER + "".join(r + "\n" for r in rows), encoding="utf-8")
        return path

    def _row(self, kind, fp):
        return f"s.test.sh\t-\t{kind}\tisolated\tfixture\tMA-FP\t{TOMORROW}\t{fp}"

    def test_current_fingerprint_row_wins_over_the_other_environment(self):
        path = self._write([self._row("exit-nonzero", "host-a"), self._row("assertion", "ci-b")])
        self.assertEqual(self.mod.load_baseline(path, "ci-b")[("s.test.sh", "-")]["expected_failure_kind"], "assertion")
        self.assertEqual(self.mod.load_baseline(path, "host-a")[("s.test.sh", "-")]["expected_failure_kind"], "exit-nonzero")

    def test_unknown_fingerprint_falls_back_to_the_first_row_as_foreign(self):
        path = self._write([self._row("exit-nonzero", "host-a"), self._row("assertion", "ci-b")])
        chosen = self.mod.load_baseline(path, "host-zzz")[("s.test.sh", "-")]
        self.assertEqual(chosen["fingerprint"], "host-a")
        self.assertFalse(self.mod.baseline_row_applicable(chosen, "host-zzz"))

    def test_same_fingerprint_twice_is_still_a_duplicate(self):
        path = self._write([self._row("exit-nonzero", "host-a"), self._row("assertion", "host-a")])
        with self.assertRaises(self.mod.BaselineError):
            self.mod.load_baseline(path, "host-a")

    def test_scoped_rows_may_not_mix_with_an_unscoped_row(self):
        path = self._write([self._row("exit-nonzero", ""), self._row("assertion", "ci-b")])
        with self.assertRaises(self.mod.BaselineError):
            self.mod.load_baseline(path, "ci-b")

    def test_single_unscoped_row_keeps_the_legacy_contract(self):
        path = self._write([self._row("exit-nonzero", "")])
        chosen = self.mod.load_baseline(path, "anything")[("s.test.sh", "-")]
        self.assertTrue(self.mod.baseline_row_applicable(chosen, "anything"))


class ExpiredFixture(RunTestsFixtureBase):
    def test_expired_review_by_is_hard_failure(self):
        write_suite(self.root, "expired.test.py", "import sys\nsys.exit(1)\n")
        result, rows = self.run_fixture(
            [f"expired.test.py\t-\texit-nonzero\tisolated\told\tMA-TEST-005\t{YESTERDAY}"]
        )
        self.assertNotEqual(result.returncode, 0)
        verdicts = {r["verdict"] for r in rows if r["suite_path"] == "expired.test.py"}
        self.assertEqual(verdicts, {"EXPIRED"})


class DuplicateRowFixture(RunTestsFixtureBase):
    def test_duplicate_row_fails_at_parse_stage(self):
        write_suite(self.root, "dup.test.py", "import sys\nsys.exit(1)\n")
        result, _rows = self.run_fixture(
            [
                f"dup.test.py\t-\texit-nonzero\tisolated\ta\tMA-TEST-006\t{TOMORROW}",
                f"dup.test.py\t-\texit-nonzero\tisolated\tb\tMA-TEST-007\t{TOMORROW}",
            ]
        )
        self.assertEqual(result.returncode, 65)
        self.assertIn("duplicate row", result.stderr)


class MissingOrMalformedHeaderFixture(RunTestsFixtureBase):
    """A TSV reader that unconditionally discards the first non-comment line
    as "the header" cannot tell a real header apart from a headerless file
    whose first data row it just silently ate."""

    def test_baseline_missing_header_is_a_typed_parse_error_not_a_dropped_row(self):
        write_suite(self.root, "b.test.py", "import sys\nsys.exit(1)\n")
        baseline = self.root / "test-baseline.tsv"
        # No header line at all -- the real first (and only) data row would
        # otherwise be silently treated as "the header" and vanish.
        baseline.write_text(
            "b.test.py\t-\texit-nonzero\tisolated\ta\tMA-NOHDR\t" + TOMORROW + "\t\n",
            encoding="utf-8",
        )
        isolation = write_isolation_tsv(self.root, None)
        result = run_runner([
            "--root", str(self.root), "--baseline", str(baseline),
            "--isolation-tsv", str(isolation), "--isolation=isolated",
            "--jobs", "1", "--timeout", "5", "--no-leak-sweep",
        ])
        self.assertEqual(result.returncode, 65, result.stdout + result.stderr)
        self.assertIn("expected header", result.stderr)

    def test_isolation_tsv_wrong_header_shape_is_a_typed_parse_error(self):
        write_suite(self.root, "b.test.py", "import sys\nsys.exit(1)\n")
        baseline = write_baseline(self.root, [])
        isolation = self.root / "test-isolation.tsv"
        # Right column count, wrong column names/order.
        isolation.write_text(
            "needs\tsuite_path\treason\tdefect_id\treview_by\n", encoding="utf-8"
        )
        result = run_runner([
            "--root", str(self.root), "--baseline", str(baseline),
            "--isolation-tsv", str(isolation), "--isolation=isolated",
            "--jobs", "1", "--timeout", "5", "--no-leak-sweep",
        ])
        self.assertEqual(result.returncode, 65, result.stdout + result.stderr)
        self.assertIn("expected header", result.stderr)


class AmbiguousRowFixture(RunTestsFixtureBase):
    def test_ambiguous_row_fails_at_parse_stage(self):
        write_suite(self.root, "amb.test.py", "import sys\nsys.exit(1)\n")
        result, _rows = self.run_fixture(
            [
                f"amb.test.py\t-\texit-nonzero\tisolated\ta\tMA-TEST-008\t{TOMORROW}",
                f"amb.test.py\tSomeClass.test_x\texit-nonzero\tisolated\tb\tMA-TEST-009\t{TOMORROW}",
            ]
        )
        self.assertEqual(result.returncode, 65)
        self.assertIn("ambiguous rows", result.stderr)


class ReviewByBoundaryFixture(RunTestsFixtureBase):
    def test_today_and_tomorrow_pass_yesterday_expires(self):
        write_suite(self.root, "b_today.test.py", "import sys\nsys.exit(1)\n")
        write_suite(self.root, "b_tomorrow.test.py", "import sys\nsys.exit(1)\n")
        write_suite(self.root, "b_yesterday.test.py", "import sys\nsys.exit(1)\n")
        result, rows = self.run_fixture(
            [
                f"b_today.test.py\t-\texit-nonzero\tisolated\tr\tMA-TEST-010\t{TODAY}",
                f"b_tomorrow.test.py\t-\texit-nonzero\tisolated\tr\tMA-TEST-011\t{TOMORROW}",
                f"b_yesterday.test.py\t-\texit-nonzero\tisolated\tr\tMA-TEST-012\t{YESTERDAY}",
            ]
        )
        by_suite = {r["suite_path"]: r["verdict"] for r in rows}
        self.assertEqual(by_suite["b_today.test.py"], "KNOWN-FAIL")
        self.assertEqual(by_suite["b_tomorrow.test.py"], "KNOWN-FAIL")
        self.assertEqual(by_suite["b_yesterday.test.py"], "EXPIRED")
        self.assertNotEqual(result.returncode, 0)


class BadDateFixture(RunTestsFixtureBase):
    def test_bad_date_format_rejected_at_parse_stage(self):
        write_suite(self.root, "bd.test.py", "import sys\nsys.exit(1)\n")
        result, _rows = self.run_fixture(
            [f"bd.test.py\t-\texit-nonzero\tisolated\tr\tMA-TEST-013\t2026/08/29"]
        )
        self.assertEqual(result.returncode, 65)
        self.assertIn("YYYY-MM-DD", result.stderr)


class EmptyDefectIdFixture(RunTestsFixtureBase):
    def test_empty_defect_id_rejected_at_parse_stage(self):
        write_suite(self.root, "ed.test.py", "import sys\nsys.exit(1)\n")
        result, _rows = self.run_fixture(
            [f"ed.test.py\t-\texit-nonzero\tisolated\tr\t\t{TOMORROW}"]
        )
        self.assertEqual(result.returncode, 65)
        self.assertIn("empty defect_id", result.stderr)


class ReportOnlyDegradeFixture(RunTestsFixtureBase):
    def test_report_only_degrades_hard_failures_to_exit_zero(self):
        write_suite(self.root, "surprise_fail.test.py", "import sys\nsys.exit(1)\n")
        write_suite(self.root, "now_passes.test.py", "import sys\nsys.exit(0)\n")
        result, rows = self.run_fixture(
            [f"now_passes.test.py\t-\texit-nonzero\tisolated\tstale\tMA-TEST-014\t{TOMORROW}"],
            report_only=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        by_suite = {r["suite_path"]: r["verdict"] for r in rows}
        self.assertEqual(by_suite["surprise_fail.test.py"], "FAIL")
        self.assertEqual(by_suite["now_passes.test.py"], "XPASS")


class SymlinkNotFollowedFixture(RunTestsFixtureBase):
    def test_collector_does_not_follow_symlinked_suite_or_directory(self):
        write_suite(self.root, "real/real.test.py", "import sys\nsys.exit(0)\n")
        looped = self.root / "loop"
        looped.symlink_to(self.root, target_is_directory=True)
        linked_file = self.root / "linked.test.py"
        linked_file.symlink_to(self.root / "real" / "real.test.py")
        result = run_runner(["--census", "--root", str(self.root)])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("collected=1", result.stdout)


class CompareBaselineFixture(RunTestsFixtureBase):
    def _write_report(self, name: str, rows: list[tuple[str, str, str]]) -> Path:
        p = self.root / name
        header = "\t".join(
            ["suite_path", "test_id", "verdict", "kind", "isolation_profile", "duration_s", "detail"]
        )
        lines = ["# report", header]
        for suite_path, test_id, verdict in rows:
            lines.append(f"{suite_path}\t{test_id}\t{verdict}\t\tisolated\t0.01\t")
        p.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return p

    def test_three_way_split_and_nonzero_exit_on_regression(self):
        base = self._write_report(
            "base.tsv",
            [
                ("a.test.py", "-", "FAIL"),
                ("b.test.py", "-", "FAIL"),
            ],
        )
        head = self._write_report(
            "head.tsv",
            [
                ("a.test.py", "-", "FAIL"),  # pre_existing
                ("c.test.py", "-", "FAIL"),  # regression
                # b.test.py absent -> fixed
            ],
        )
        result = run_runner(["--compare-baseline", "--base-report", str(base), "--head-report", str(head)])
        self.assertNotEqual(result.returncode, 0)
        payload = json.loads(result.stdout)
        self.assertEqual(
            {(d["suite_path"], d["test_id"]) for d in payload["pre_existing"]}, {("a.test.py", "-")}
        )
        self.assertEqual(
            {(d["suite_path"], d["test_id"]) for d in payload["regression"]}, {("c.test.py", "-")}
        )
        self.assertEqual({(d["suite_path"], d["test_id"]) for d in payload["fixed"]}, {("b.test.py", "-")})

    def test_no_regression_exits_zero(self):
        base = self._write_report("base2.tsv", [("a.test.py", "-", "FAIL")])
        head = self._write_report("head2.tsv", [("a.test.py", "-", "FAIL")])
        result = run_runner(["--compare-baseline", "--base-report", str(base), "--head-report", str(head)])
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_known_fail_to_flaky_known_fail_is_not_regression(self):
        base = self._write_report("base-flaky.tsv", [("a.test.py", "-", "KNOWN-FAIL")])
        head = self._write_report("head-flaky.tsv", [("a.test.py", "-", "FLAKY-KNOWN-FAIL")])
        result = run_runner(["--compare-baseline", "--base-report", str(base), "--head-report", str(head)])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["regression"], [])

    def test_flaky_known_fail_to_pass_is_a_fix_not_regression(self):
        base = self._write_report("base-flaky-pass.tsv", [("a.test.py", "-", "FLAKY-KNOWN-FAIL")])
        head = self._write_report("head-flaky-pass.tsv", [("a.test.py", "-", "PASS")])
        result = run_runner(["--compare-baseline", "--base-report", str(base), "--head-report", str(head)])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["fixed"], [{"suite_path": "a.test.py", "test_id": "-"}])


class SuiteScratchLifetimeFixture(RunTestsFixtureBase):
    def test_suite_scratch_is_removed_before_the_next_suite_starts(self):
        prior_root = self.root / "prior-suite-root.txt"
        write_suite(self.root, "a_first.test.py", f"""
            import os
            from pathlib import Path
            Path({str(prior_root)!r}).write_text(str(Path(os.environ['HOME']).parent))
        """)
        write_suite(self.root, "b_second.test.py", f"""
            from pathlib import Path
            prior = Path(Path({str(prior_root)!r}).read_text())
            assert not prior.exists(), f'prior suite scratch still exists: {{prior}}'
        """)

        result, rows = self.run_fixture([], extra_args=["--jobs", "1"])

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual([row["verdict"] for row in rows], ["PASS", "PASS"])
        self.assertFalse(Path(prior_root.read_text()).exists())

    def test_failure_diagnostics_survive_attempt_scratch_cleanup(self):
        prior_root = self.root / "failed-suite-root.txt"
        diagnostics = self.root / "diagnostics"
        write_suite(self.root, "fail.test.py", f"""
            import os, sys
            from pathlib import Path
            Path({str(prior_root)!r}).write_text(str(Path(os.environ['HOME']).parent))
            print('attempt stdout retained')
            print('attempt stderr retained', file=sys.stderr)
            sys.exit(1)
        """)

        result, rows = self.run_fixture(
            [], extra_args=["--jobs", "1", "--diagnostics-dir", str(diagnostics)],
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(rows[0]["verdict"], "FAIL")
        self.assertFalse(Path(prior_root.read_text()).exists())
        index = (diagnostics / "index.tsv").read_text(encoding="utf-8")
        self.assertIn("fail.test.py", index)
        stdout_path = next(diagnostics.glob("*/attempt-001.stdout.txt"))
        stderr_path = next(diagnostics.glob("*/attempt-001.stderr.txt"))
        self.assertIn("attempt stdout retained", stdout_path.read_text(encoding="utf-8"))
        self.assertIn("attempt stderr retained", stderr_path.read_text(encoding="utf-8"))

    def test_unrelated_unreadable_process_does_not_block_root_but_owned_unknown_does(self):
        mod = load_runner_module()
        real_read_bytes = Path.read_bytes
        real_listdir = os.listdir

        def unreadable_unrelated(path):
            if str(path) == "/proc/88001/stat":
                raise PermissionError("unrelated stat is hidden")
            return real_read_bytes(path)

        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "unrelated"
            root.mkdir()
            identity = mod.register_fixture_root(root)
            with mock.patch.object(os, "listdir", side_effect=lambda p: ["88001"] if p == "/proc" else real_listdir(p)), \
                    mock.patch.object(Path, "read_bytes", unreadable_unrelated):
                self.assertTrue(mod.cleanup_suite_tmp_root(root, expected_identity=identity))
            self.assertFalse(root.exists())

            owned = Path(td) / "owned"
            owned.mkdir()
            owned_identity = mod.register_fixture_root(owned)
            mod._remember_fixture_process(str(owned), 88002, "78")

            def unreadable_owned(path):
                if str(path) == "/proc/88002/stat":
                    raise PermissionError("observed owned stat is hidden")
                return real_read_bytes(path)

            with mock.patch.object(os, "listdir", side_effect=lambda p: ["88002"] if p == "/proc" else real_listdir(p)), \
                    mock.patch.object(Path, "read_bytes", unreadable_owned):
                self.assertFalse(mod.cleanup_suite_tmp_root(owned, expected_identity=owned_identity))
            self.assertTrue(owned.is_dir())

    def test_process_group_observation_tracks_descendants_but_not_unmarked_bystanders(self):
        mod = load_runner_module()
        real_read_bytes = Path.read_bytes
        real_listdir = os.listdir
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "group"
            root.mkdir()
            identity = mod.register_fixture_root(root)
            phase = {"settled": False}

            def make_stat(pid, state, ppid, ticks):
                fields = [b"0"] * 20
                fields[0] = state.encode()
                fields[1] = str(ppid).encode()
                fields[2] = b"440"
                fields[19] = str(ticks).encode()
                return f"{pid} (fixture) ".encode() + b" ".join(fields)

            def fake_read_bytes(path):
                name = str(path)
                if name == "/proc/88010/stat":
                    return make_stat(88010, "Z" if phase["settled"] else "S", 1, 10)
                if name == "/proc/88012/stat":
                    return make_stat(88012, "Z" if phase["settled"] else "S", 88010, 12)
                if name == "/proc/88011/stat":
                    if phase["settled"]:
                        raise PermissionError("unmarked same-group bystander is unreadable")
                    return make_stat(88011, "S", 99999, 11)
                if name.endswith(("/cmdline", "/environ")):
                    return b""
                return real_read_bytes(path)

            with mock.patch.object(os, "listdir", side_effect=lambda p: ["88010", "88011", "88012"] if p == "/proc" else real_listdir(p)), \
                    mock.patch.object(Path, "read_bytes", fake_read_bytes), \
                    mock.patch.object(os, "kill") as kill:
                mod._remember_process_group(440, [str(root)], owner_pid=88010)
                self.assertEqual(
                    mod._FIXTURE_PROCESS_IDENTITIES[str(root)],
                    {88010: "10", 88012: "12"},
                )
                phase["settled"] = True
                self.assertTrue(mod.cleanup_suite_tmp_root(root, expected_identity=identity))
                kill.assert_not_called()

    def test_unreadable_child_stderr_keeps_attempt_root_after_profile_finally(self):
        import shutil

        mod = load_runner_module()
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            suite = root / "suite.test.py"
            suite.touch()
            attempt_root = {}

            def fake_run(_suite, _root, env, profile, _timeout):
                attempt = Path(env[mod.CHILD_STDERR_ENV]).parent
                attempt_root["path"] = attempt
                Path(env[mod.CHILD_STDERR_ENV], "unreadable.log").write_text("keep")
                return mod.SuiteResult("suite.test.py", 1, False, "out", "err", .01, profile)

            with mock.patch.object(mod, "run_suite", side_effect=fake_run), \
                    mock.patch.object(mod, "reap_suite_tmp_processes", return_value=[]), \
                    mock.patch.object(mod, "collect_child_stderr", side_effect=PermissionError("fixture log hidden")):
                results = mod.run_profile([suite], root, "isolated", 1, 5, None)

            self.assertEqual(results[0].returncode, 1)
            self.assertTrue(attempt_root["path"].is_dir())
            self.assertEqual(
                (attempt_root["path"] / "child-stderr" / "unreadable.log").read_text(),
                "keep",
            )
            shutil.rmtree(attempt_root["path"], ignore_errors=True)

    def test_live_marked_process_and_replaced_root_are_preserved(self):
        mod = load_runner_module()
        real_read_bytes = Path.read_bytes
        real_listdir = os.listdir

        with tempfile.TemporaryDirectory() as td:
            parent = Path(td)
            root = parent / "live"
            root.mkdir()
            identity = mod.register_fixture_root(root)
            marker = os.fsencode(str(root))
            fields = [b"0"] * 20
            fields[0] = b"S"
            fields[2] = b"1"
            fields[19] = b"79"
            proc_stat = b"88003 (fixture) " + b" ".join(fields)

            def read_live(path):
                if str(path) == "/proc/88003/stat":
                    return proc_stat
                if str(path) == "/proc/88003/cmdline":
                    return b""
                if str(path) == "/proc/88003/environ":
                    return b"TMPDIR=" + marker
                return real_read_bytes(path)

            with mock.patch.object(os, "listdir", side_effect=lambda p: ["88003"] if p == "/proc" else real_listdir(p)), \
                    mock.patch.object(Path, "read_bytes", read_live), \
                    mock.patch.object(os, "kill"):
                self.assertFalse(mod.cleanup_suite_tmp_root(root, expected_identity=identity, timeout=0))
            self.assertTrue(root.is_dir())

            external = parent / "external"
            sibling = parent / "sibling"
            external.mkdir()
            sibling.mkdir()
            (external / "sentinel").write_text("keep")
            replaced = parent / "replaced"
            replaced.mkdir()
            replaced_identity = mod.register_fixture_root(replaced)
            replaced.rmdir()
            replaced.symlink_to(external, target_is_directory=True)
            self.assertFalse(mod.cleanup_suite_tmp_root(replaced, expected_identity=replaced_identity))
            self.assertEqual((external / "sentinel").read_text(), "keep")
            self.assertTrue(sibling.is_dir())
            self.assertTrue(replaced.is_symlink())


class SuiteScratchExceptionFinalizationFixture(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.mod = load_runner_module()

    def tearDown(self):
        self._tmp.cleanup()

    def make_attempt(self, name):
        attempt_root = self.root / name
        log_dir = attempt_root / "child-stderr"
        log_dir.mkdir(parents=True)
        identity = self.mod.register_fixture_root(attempt_root)
        log = log_dir / "child.log"
        log.write_text("early child stderr\n", encoding="utf-8")
        env = {self.mod.CHILD_STDERR_ENV: str(log_dir)}
        return attempt_root, identity, log, env

    def recording_stderr(self, attempt_root):
        class RecordingStderr(io.StringIO):
            root_was_live_when_consumed = False

            def write(inner_self, value):
                if "final child stderr" in value:
                    inner_self.root_was_live_when_consumed = attempt_root.is_dir()
                return super(RecordingStderr, inner_self).write(value)

        return RecordingStderr()

    def test_runtime_error_and_keyboard_interrupt_finalize_then_reraise_original(self):
        for error in (RuntimeError("synthetic runner failure"), KeyboardInterrupt()):
            with self.subTest(error=type(error).__name__):
                attempt_root, identity, log, env = self.make_attempt(type(error).__name__)
                events = []
                stderr = self.recording_stderr(attempt_root)

                def settle(_tmp_root):
                    events.append("settled")
                    with log.open("a", encoding="utf-8") as stream:
                        stream.write("final child stderr\n")
                    return []

                with mock.patch.object(self.mod, "run_suite", side_effect=error), \
                        mock.patch.object(self.mod, "reap_suite_tmp_processes", side_effect=settle), \
                        mock.patch.object(self.mod.sys, "stderr", stderr):
                    with self.assertRaises(type(error)) as raised:
                        self.mod.run_suite_with_scratch(
                            self.root / "suite.test.py", self.root, env,
                            "isolated", 1, attempt_root, identity,
                        )

                self.assertIs(raised.exception, error)
                self.assertIn("early child stderr", stderr.getvalue())
                self.assertIn("final child stderr", stderr.getvalue())
                self.assertTrue(stderr.root_was_live_when_consumed)
                self.assertEqual(events, ["settled"])
                self.assertFalse(attempt_root.exists())

    def test_popen_failure_uses_common_finalizer_and_reraises_original(self):
        attempt_root, identity, log, env = self.make_attempt("popen-failure")
        suite = write_suite(self.root, "spawn-failure.test.py", "print('not launched')")
        error = FileNotFoundError("synthetic Popen failure")
        stderr = self.recording_stderr(attempt_root)

        def settle(_tmp_root):
            with log.open("a", encoding="utf-8") as stream:
                stream.write("final child stderr\n")
            return []

        with mock.patch.object(self.mod, "_suite_progress"), \
                mock.patch.object(self.mod.subprocess, "Popen", side_effect=error), \
                mock.patch.object(self.mod, "reap_suite_tmp_processes", side_effect=settle), \
                mock.patch.object(self.mod.sys, "stderr", stderr):
            with self.assertRaises(FileNotFoundError) as raised:
                self.mod.run_suite_with_scratch(
                    suite, self.root, env, "isolated", 1, attempt_root, identity,
                )

        self.assertIs(raised.exception, error)
        self.assertIn("final child stderr", stderr.getvalue())
        self.assertTrue(stderr.root_was_live_when_consumed)
        self.assertFalse(attempt_root.exists())

    def test_cleanup_baseexception_does_not_replace_runner_exception(self):
        attempt_root, identity, log, env = self.make_attempt("cleanup-interrupted")
        error = RuntimeError("original suite error")
        stderr = self.recording_stderr(attempt_root)

        with mock.patch.object(self.mod, "run_suite", side_effect=error), \
                mock.patch.object(
                    self.mod, "reap_suite_tmp_processes",
                    side_effect=KeyboardInterrupt("cleanup interrupted"),
                ), \
                mock.patch.object(self.mod.sys, "stderr", stderr):
            with self.assertRaises(RuntimeError) as raised:
                self.mod.run_suite_with_scratch(
                    self.root / "suite.test.py", self.root, env,
                    "isolated", 1, attempt_root, identity,
                )

        self.assertIs(raised.exception, error)
        self.assertTrue(attempt_root.is_dir())
        self.assertTrue(log.is_file())
        self.assertIn("interrupted-finalization-error", stderr.getvalue())

    def test_live_unknown_and_unreadable_output_preserve_scratch_and_diagnostics(self):
        cases = (
            ("live", [88031], False, "owned-process-live-or-unreadable"),
            ("unknown", None, False, "process-state-unreadable"),
            ("unreadable-output", [], True, "child-stderr-unreadable"),
        )
        for name, settlement, unreadable, reason in cases:
            with self.subTest(name=name):
                attempt_root, identity, log, env = self.make_attempt(name)
                error = RuntimeError(f"{name} runner error")
                stderr = self.recording_stderr(attempt_root)

                def settle(_tmp_root):
                    if not unreadable:
                        with log.open("a", encoding="utf-8") as stream:
                            stream.write("final child stderr\n")
                    return settlement

                patches = [
                    mock.patch.object(self.mod, "run_suite", side_effect=error),
                    mock.patch.object(self.mod, "reap_suite_tmp_processes", side_effect=settle),
                    mock.patch.object(self.mod.sys, "stderr", stderr),
                ]
                if unreadable:
                    patches.append(mock.patch.object(
                        self.mod, "collect_child_stderr",
                        side_effect=PermissionError("fixture child log unreadable"),
                    ))
                with contextlib.ExitStack() as stack:
                    for patcher in patches:
                        stack.enter_context(patcher)
                    with self.assertRaises(RuntimeError) as raised:
                        self.mod.run_suite_with_scratch(
                            self.root / "suite.test.py", self.root, env,
                            "isolated", 1, attempt_root, identity,
                        )

                self.assertIs(raised.exception, error)
                self.assertTrue(attempt_root.is_dir())
                self.assertIn(reason, stderr.getvalue())
                if unreadable:
                    self.assertNotIn("final child stderr", stderr.getvalue())
                    self.assertTrue(log.is_file())
                else:
                    self.assertIn("final child stderr", stderr.getvalue())
                    self.assertTrue(stderr.root_was_live_when_consumed)

    def test_failed_diagnostic_consumer_preserves_scratch_and_original_exception(self):
        attempt_root, identity, log, env = self.make_attempt("consumer-failure")
        error = RuntimeError("original suite error")

        class BrokenStderr(io.StringIO):
            def write(inner_self, value):
                if "final child stderr" in value:
                    raise BrokenPipeError("diagnostic output closed")
                return super(BrokenStderr, inner_self).write(value)

        stderr = BrokenStderr()

        def settle(_tmp_root):
            with log.open("a", encoding="utf-8") as stream:
                stream.write("final child stderr\n")
            return []

        with mock.patch.object(self.mod, "run_suite", side_effect=error), \
                mock.patch.object(self.mod, "reap_suite_tmp_processes", side_effect=settle), \
                mock.patch.object(self.mod.sys, "stderr", stderr):
            with self.assertRaises(RuntimeError) as raised:
                self.mod.run_suite_with_scratch(
                    self.root / "suite.test.py", self.root, env,
                    "isolated", 1, attempt_root, identity,
                )

        self.assertIs(raised.exception, error)
        self.assertTrue(attempt_root.is_dir())
        self.assertTrue(log.is_file())
        self.assertIn("child-stderr-consumer-failed", stderr.getvalue())

    def test_replaced_root_preserves_external_target_and_sibling_on_interruption(self):
        attempt_root, identity, _log, env = self.make_attempt("replaced-root")
        external = self.root / "external"
        sibling = self.root / "sibling"
        external.mkdir()
        sibling.mkdir()
        sentinel = external / "sentinel"
        sentinel.write_text("keep", encoding="utf-8")
        error = RuntimeError("suite failed after replacing its scratch root")
        stderr = self.recording_stderr(attempt_root)

        def replace_then_raise(*_args):
            import shutil
            shutil.rmtree(attempt_root)
            attempt_root.symlink_to(external, target_is_directory=True)
            raise error

        with mock.patch.object(self.mod, "run_suite", side_effect=replace_then_raise), \
                mock.patch.object(self.mod.sys, "stderr", stderr):
            with self.assertRaises(RuntimeError) as raised:
                self.mod.run_suite_with_scratch(
                    self.root / "suite.test.py", self.root, env,
                    "isolated", 1, attempt_root, identity,
                )

        self.assertIs(raised.exception, error)
        self.assertTrue(attempt_root.is_symlink())
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep")
        self.assertTrue(sibling.is_dir())
        self.assertIn("root-identity-changed", stderr.getvalue())


class InstalledLayoutLifetimeFixture(RunTestsFixtureBase):
    def test_shared_layout_lives_through_diagnostics_then_is_removed(self):
        mod = load_runner_module()
        layout_file = self.root / "installed-layout.txt"
        diagnostics = self.root / "diagnostics"
        write_suite(self.root, "installed_failure.test.py", f"""
            import os, sys
            from pathlib import Path
            layout = Path(os.environ['AGENT_HOME'])
            assert layout.exists(), f'installed layout missing during suite: {{layout}}'
            Path({str(layout_file)!r}).write_text(str(layout))
            print('installed-layout attempt output')
            sys.exit(1)
        """)
        baseline = write_baseline(self.root, [])
        isolation = write_isolation_tsv(self.root, [])
        prefix_holder = []
        original_writer = mod.write_failure_diagnostics
        original_mkdtemp = tempfile.mkdtemp

        def make_stub_layout():
            prefix = Path(original_mkdtemp(prefix="runner-layout-stub-"))
            (prefix / "home" / ".claude").mkdir(parents=True)
            identity = mod.register_fixture_root(prefix)
            mod._INSTALLED_LAYOUT_CACHE.update(prefix=prefix, identity=identity)
            prefix_holder.append(prefix)
            return prefix

        def diagnostics_with_layout(*args, **kwargs):
            self.assertTrue(prefix_holder[-1].is_dir())
            return original_writer(*args, **kwargs)

        args = [
            "--root", str(self.root), "--baseline", str(baseline),
            "--isolation-tsv", str(isolation), "--isolation=installed-layout",
            "--no-leak-sweep", "--report", str(self.root / "report.tsv"),
            "--diagnostics-dir", str(diagnostics),
        ]
        with mock.patch.object(mod, "get_installed_layout_prefix", side_effect=make_stub_layout), \
                mock.patch.object(mod, "write_failure_diagnostics", side_effect=diagnostics_with_layout):
            self.assertEqual(mod.main(args), 1)

        self.assertTrue((diagnostics / "index.tsv").is_file())
        stdout_path = next(diagnostics.glob("*/attempt-001.stdout.txt"))
        self.assertIn("installed-layout attempt output", stdout_path.read_text(encoding="utf-8"))
        self.assertTrue(prefix_holder)
        self.assertEqual(
            layout_file.read_text(encoding="utf-8"),
            str(prefix_holder[0] / "home" / ".claude"),
        )
        self.assertFalse(prefix_holder[0].exists())
        self.assertEqual(mod._INSTALLED_LAYOUT_CACHE, {})

    def test_main_keeps_layout_through_retry_probe_and_diagnostics_then_expires_cache(self):
        mod = load_runner_module()
        probe_seen = self.root / "probe-layout.txt"
        diagnostics = self.root / "diagnostics"
        write_suite(self.root, "declared.test.py", """
            import os
            from pathlib import Path
            assert Path(os.environ["AGENT_HOME"]).is_dir()
        """)
        write_suite(self.root, "probe.test.py", f"""
            import os, sys
            from pathlib import Path
            home = os.environ.get("AGENT_HOME", "")
            present = bool(home and Path(home).is_dir())
            Path({str(probe_seen)!r}).write_text("present" if present else "missing")
            print("probe output", "installed" if present else "isolated")
            if not present:
                sys.exit(1)
        """)
        baseline = write_baseline(self.root, [])
        isolation = write_isolation_tsv(self.root, [
            f"declared.test.py\tinstalled-layout\tfixture\tMA-LAYOUT\t{TOMORROW}"
        ])
        prefix_holder = []
        original_writer = mod.write_failure_diagnostics
        original_mkdtemp = tempfile.mkdtemp

        def make_stub_layout():
            prefix = Path(original_mkdtemp(prefix="runner-layout-stub-"))
            (prefix / "home" / ".claude").mkdir(parents=True)
            identity = mod.register_fixture_root(prefix)
            mod._INSTALLED_LAYOUT_CACHE["prefix"] = prefix
            mod._INSTALLED_LAYOUT_CACHE["identity"] = identity
            prefix_holder.append(prefix)
            return prefix

        def diagnostics_after_probe(*args, **kwargs):
            self.assertTrue(prefix_holder[-1].is_dir(), "layout removed before diagnostics consumed attempts")
            return original_writer(*args, **kwargs)

        args = [
            "--root", str(self.root), "--baseline", str(baseline),
            "--isolation-tsv", str(isolation),
            "--jobs", "1", "--timeout", "5", "--retries", "1",
            "--no-leak-sweep", "--report", str(self.root / "report.tsv"),
            "--diagnostics-dir", str(diagnostics),
        ]
        with mock.patch.object(mod, "get_installed_layout_prefix", side_effect=make_stub_layout), \
                mock.patch.object(mod, "write_failure_diagnostics", side_effect=diagnostics_after_probe):
            self.assertEqual(mod.main(args), 1)  # the unlisted isolated failure is reported

        self.assertEqual(probe_seen.read_text(), "present")
        self.assertTrue(prefix_holder)
        self.assertFalse(prefix_holder[0].exists())
        self.assertEqual(mod._INSTALLED_LAYOUT_CACHE, {})
        index = (diagnostics / "index.tsv").read_text(encoding="utf-8")
        self.assertIn("probe.test.py", index)
        probe_attempts = [line for line in index.splitlines()[1:] if "probe.test.py" in line]
        self.assertGreaterEqual(len(probe_attempts), 2)

    def test_partial_layout_build_and_suite_preparation_failures_release_created_roots(self):
        mod = load_runner_module()
        write_suite(self.root, "installed.test.py", "pass\n")
        baseline = write_baseline(self.root, [])
        isolation = write_isolation_tsv(self.root, [])
        layout_roots = []
        original_mkdtemp = tempfile.mkdtemp
        original_build = mod.build_isolated_env

        def track_layout_mkdtemp(*args, **kwargs):
            value = original_mkdtemp(*args, **kwargs)
            if kwargs.get("prefix", "").startswith("w1-installed-layout-"):
                layout_roots.append(Path(value))
            return value

        def fail_layout_build(tmpdir, *args, **kwargs):
            if Path(tmpdir).name == "build-env":
                raise RuntimeError("stub partial layout build failure")
            return original_build(tmpdir, *args, **kwargs)

        args = [
            "--root", str(self.root), "--baseline", str(baseline),
            "--isolation-tsv", str(isolation), "--isolation=installed-layout",
            "--no-leak-sweep", "--report", str(self.root / "report.tsv"),
        ]
        with mock.patch.object(mod.tempfile, "mkdtemp", side_effect=track_layout_mkdtemp), \
                mock.patch.object(mod, "build_isolated_env", side_effect=fail_layout_build):
            with self.assertRaisesRegex(RuntimeError, "partial layout build failure"):
                mod.main(args)
        self.assertEqual(len(layout_roots), 1)
        self.assertFalse(layout_roots[0].exists())
        self.assertEqual(mod._INSTALLED_LAYOUT_CACHE, {})

        prep_mod = load_runner_module()
        prep_roots = []

        def track_attempt_mkdtemp(*args, **kwargs):
            value = original_mkdtemp(*args, **kwargs)
            if kwargs.get("prefix", "").startswith("w1-isolated-"):
                prep_roots.append(Path(value))
            return value

        with mock.patch.object(prep_mod.tempfile, "mkdtemp", side_effect=track_attempt_mkdtemp), \
                mock.patch.object(prep_mod, "build_isolated_env", side_effect=RuntimeError("suite prep failed")):
            with self.assertRaisesRegex(RuntimeError, "suite prep failed"):
                prep_mod.run_profile(
                    [self.root / "installed.test.py"], self.root, "isolated", 1, 5, None
                )
        self.assertEqual(len(prep_roots), 1)
        self.assertFalse(prep_roots[0].exists())

    def test_catchable_interruption_expires_cached_layout(self):
        mod = load_runner_module()
        prefix = Path(tempfile.mkdtemp(prefix="runner-layout-interrupt-"))
        (prefix / "home").mkdir()
        identity = mod.register_fixture_root(prefix)
        mod._INSTALLED_LAYOUT_CACHE.update(prefix=prefix, identity=identity)
        with mock.patch.object(mod, "_run_main", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                mod.main([])
        self.assertFalse(prefix.exists())
        self.assertEqual(mod._INSTALLED_LAYOUT_CACHE, {})


class RetryFixture(RunTestsFixtureBase):
    def write_retry_suite(self, mode: str):
        write_suite(self.root, "retry.test.py", f"""
            from pathlib import Path
            import sys
            p = Path('attempts.txt')
            n = int(p.read_text()) + 1 if p.exists() else 1
            p.write_text(str(n))
            mode = {mode!r}
            if mode == 'fail-pass' and n == 1: sys.exit(1)
            if mode == 'pass-fail' and n > 1: sys.exit(1)
            if mode == 'all-fail': sys.exit(1)
            if mode == 'mismatch': raise ImportError('fixture mismatch')
        """)

    def baseline(self, kind="exit-nonzero"):
        return [f"retry.test.py\t-\t{kind}\tisolated\tflaky-timing: intermittent\tMA-RETRY\t{TOMORROW}"]

    def test_fail_pass_and_pass_fail_are_flaky(self):
        for mode in ("fail-pass", "pass-fail"):
            with self.subTest(mode=mode):
                (self.root / "attempts.txt").unlink(missing_ok=True)
                self.write_retry_suite(mode)
                result, rows = self.run_fixture(self.baseline(), extra_args=["--retries", "1"])
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual([r["verdict"] for r in rows], ["FLAKY-KNOWN-FAIL"])

    def test_all_pass_is_flaky_pass_not_a_hard_failure(self):
        # A flaky-timing suite's all-pass aggregate proves nothing about a
        # suite explicitly declared flaky -- it gets a distinct non-fatal
        # verdict instead of the non-flaky XPASS hard failure.
        self.write_retry_suite("all-pass")
        result, rows = self.run_fixture(self.baseline(), extra_args=["--retries", "1"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(rows[0]["verdict"], "FLAKY-PASS")

    def test_all_fail_is_known_fail(self):
        self.write_retry_suite("all-fail")
        result, rows = self.run_fixture(self.baseline(), extra_args=["--retries", "1"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(rows[0]["verdict"], "KNOWN-FAIL")

    def test_non_flaky_baseline_runs_once_even_with_retries(self):
        self.write_retry_suite("all-fail")
        result, _rows = self.run_fixture(
            [f"retry.test.py\t-\texit-nonzero\tisolated\tordinary\tMA-RETRY-ONCE\t{TOMORROW}"],
            extra_args=["--retries", "3"],
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.root / "attempts.txt").read_text(), "1")

    def test_unlisted_failure_that_passes_alone_is_flaky_pass(self):
        # A suite with no baseline row that fails in the main run gets one serial
        # retry; passing alone means load/timing, reported but not fatal.
        self.write_retry_suite("fail-pass")
        result, rows = self.run_fixture([], extra_args=["--retries", "1"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(rows[0]["verdict"], "FLAKY-PASS")
        self.assertIn("policy=unlisted-serial-retry", rows[0]["detail"])
        self.assertEqual((self.root / "attempts.txt").read_text(), "2")

    def test_unlisted_failure_that_fails_again_still_fails(self):
        self.write_retry_suite("all-fail")
        result, rows = self.run_fixture([], extra_args=["--retries", "1"])
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(rows[0]["verdict"], "FAIL")
        self.assertEqual((self.root / "attempts.txt").read_text(), "2")

    def test_kind_mismatch_wins_over_flaky_aggregate(self):
        self.write_retry_suite("mismatch")
        result, rows = self.run_fixture(self.baseline(), extra_args=["--retries", "1"])
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(rows[0]["verdict"], "KIND-MISMATCH")

    def test_flaky_detail_and_exit_code(self):
        self.write_retry_suite("fail-pass")
        result, rows = self.run_fixture(self.baseline(), extra_args=["--retries", "1"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(rows[0]["detail"], "attempts=2; outcomes=known-fail,pass; policy=flaky-timing")

    def test_diagnostics_preserve_every_retry_attempt(self):
        write_suite(self.root, "retry.test.py", """
            from pathlib import Path
            import subprocess, sys
            p = Path('attempts.txt')
            n = int(p.read_text()) + 1 if p.exists() else 1
            p.write_text(str(n))
            subprocess.run([
                sys.executable, "-c",
                "import sys; sys.stderr.write('child stderr attempt ' + sys.argv[1]); sys.exit(7)",
                str(n),
            ], capture_output=True, text=True)
            print(f'run stdout attempt {n}')
            print(f'run stderr attempt {n}', file=sys.stderr)
            if n == 1:
                sys.exit(1)
        """)
        diagnostics = self.root / "diagnostics"
        result, rows = self.run_fixture(
            self.baseline(),
            extra_args=["--retries", "1", "--diagnostics-dir", str(diagnostics)],
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(rows[0]["verdict"], "FLAKY-KNOWN-FAIL")
        lines = (diagnostics / "index.tsv").read_text(encoding="utf-8").splitlines()
        header = lines[0].split("\t")
        attempts = [dict(zip(header, line.split("\t"))) for line in lines[1:]]
        self.assertEqual([row["attempt"] for row in attempts], ["1", "2"])
        self.assertEqual([row["kind"] for row in attempts], ["exit-nonzero", ""])
        for number, row in enumerate(attempts, start=1):
            self.assertIn(
                f"run stdout attempt {number}",
                (diagnostics / row["stdout_path"]).read_text(encoding="utf-8"),
            )
            self.assertIn(
                f"run stderr attempt {number}",
                (diagnostics / row["stderr_path"]).read_text(encoding="utf-8"),
            )
        child_stderr = next(diagnostics.glob("*/attempt-001.child-stderr.txt"))
        self.assertIn("child stderr attempt 1", child_stderr.read_text(encoding="utf-8"))
        self.assertFalse(list(diagnostics.glob("*/attempt-002.child-stderr.txt")))

    def test_retry_budget_reaches_short_suites_before_long_suites(self):
        order = self.root / "retry-order.txt"
        for name, delay in (("a-slow", .20), ("b-fast", .01)):
            write_suite(self.root, name + ".test.py", f"""
                from pathlib import Path
                import sys, time
                count = Path({(name + '.count')!r})
                n = int(count.read_text()) + 1 if count.exists() else 1
                count.write_text(str(n))
                if n == 1:
                    time.sleep({delay})
                    sys.exit(1)
                with open({str(order)!r}, 'a') as stream: stream.write({name!r} + '\\n')
            """)
        result, _rows = self.run_fixture([], extra_args=["--retries", "1", "--retry-budget", "1"])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(order.read_text().splitlines(), ["b-fast", "a-slow"])


class ChildStderrDiagnosticsFixture(RunTestsFixtureBase):
    """A failing suite's diagnostics keep the stderr of a child it captured.

    The fixture asserts only on the child's stdout, as the completion-marker
    suite did when the route guard's reason never reached the CI artifact.
    """

    SUITE = """\
        import subprocess, sys
        for _ in range(3):
            child = subprocess.run(
                [sys.executable, "-c",
                 "import sys; print('check=failed'); sys.exit('guard reason: tuple drifted')"],
                capture_output=True, text=True,
            )
        quiet = subprocess.run([sys.executable, "-c", "import sys; sys.stderr.write('passing noise')"],
                               capture_output=True, text=True)
        assert quiet.returncode == 0
        assert "reason=expected" in child.stdout, child.stdout
    """

    def test_failing_suite_keeps_captured_child_stderr(self):
        write_suite(self.root, "swallow.test.py", self.SUITE)
        diagnostics = self.root / "diagnostics"
        result, rows = self.run_fixture([], extra_args=["--diagnostics-dir", str(diagnostics)])
        self.assertEqual(rows[0]["verdict"], "FAIL", result.stdout + result.stderr)
        kept = list(diagnostics.glob("*/attempt-001.child-stderr.txt"))
        self.assertEqual(len(kept), 1, sorted(p.name for p in diagnostics.rglob("*")))
        text = kept[0].read_text(encoding="utf-8")
        self.assertIn("guard reason: tuple drifted", text)
        # Three identical failures in one process are kept once.
        self.assertEqual(text.count("--- rc=1 "), 1, text)
        self.assertNotIn("passing noise", text)

    def test_passing_suite_writes_no_child_stderr(self):
        write_suite(self.root, "swallow.test.py",
                    self.SUITE.replace('"reason=expected" in', '"check=failed" in'))
        diagnostics = self.root / "diagnostics"
        result, rows = self.run_fixture([], extra_args=["--diagnostics-dir", str(diagnostics)])
        self.assertEqual(rows[0]["verdict"], "PASS", result.stdout + result.stderr)
        self.assertEqual(list(diagnostics.glob("*/*.child-stderr.txt")), [])

    def test_hook_leaves_the_callers_result_unchanged(self):
        runner = load_runner_module()
        with tempfile.TemporaryDirectory() as td:
            env = runner.build_isolated_env(Path(td))
            probe = subprocess.run(
                [sys.executable, "-c",
                 "import subprocess, sys; r = subprocess.run([sys.executable, '-c', "
                 "'import sys; sys.exit(\"why\")'], capture_output=True, text=True); "
                 "print(r.returncode, repr(r.stderr))"],
                env=env, capture_output=True, text=True, timeout=60,
            )
            self.assertEqual(probe.stdout.strip(), "1 'why\\n'", probe.stderr)
            self.assertIn("why", runner.collect_child_stderr(env))
            self.assertEqual(runner.collect_child_stderr(env), "")

    def test_stderr_written_during_owned_process_settlement_is_in_the_result(self):
        runner = load_runner_module()
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "attempt"
            log_dir = root / "child-stderr"
            log_dir.mkdir(parents=True)
            identity = runner.register_fixture_root(root)
            log = log_dir / "late.log"
            log.write_text("early stderr\n", encoding="utf-8")
            env = {runner.CHILD_STDERR_ENV: str(log_dir)}
            result = runner.SuiteResult(
                "late.test.py", 1, False, "out", "err", .01, "isolated"
            )

            def settle_then_write(_tmp_root):
                with log.open("a", encoding="utf-8") as stream:
                    stream.write("late stderr after suite return\n")
                return []

            with mock.patch.object(runner, "reap_suite_tmp_processes", side_effect=settle_then_write):
                finalized = runner.finalize_suite_attempt(result, env, root, identity)

            self.assertIn("early stderr", finalized.child_stderr)
            self.assertIn("late stderr after suite return", finalized.child_stderr)
            self.assertFalse(root.exists())


class DiagnosticConfinementTest(RunTestsFixtureBase):
    def test_symlinks_are_rejected_without_external_writes(self):
        mod = load_runner_module()
        result = mod.SuiteResult("fixture.test.py", 1, False, "stdout", "stderr", 0.1, "isolated")
        for target in ("root", "suite", "stdout", "stderr", "index"):
            with self.subTest(target=target), tempfile.TemporaryDirectory() as tmp:
                base = Path(tmp)
                output = base / "diagnostics"
                outside = base / "outside"
                outside.mkdir()
                sentinel = outside / "sentinel"
                sentinel.write_text("unchanged")
                if target == "root":
                    output.symlink_to(outside, target_is_directory=True)
                else:
                    # First produce valid paths, then poison one path for reuse.
                    mod.write_failure_diagnostics(output, {result.relpath: [result]})
                    suite = next(p for p in output.iterdir() if p.is_dir())
                    if target == "suite":
                        import shutil
                        shutil.rmtree(suite)
                        suite.symlink_to(outside, target_is_directory=True)
                    else:
                        victim = output / "index.tsv" if target == "index" else suite / f"attempt-001.{target}.txt"
                        victim.unlink()
                        victim.symlink_to(sentinel)
                with self.assertRaises(OSError):
                    mod.write_failure_diagnostics(output, {result.relpath: [result]})
                self.assertEqual(sentinel.read_text(), "unchanged")
                self.assertEqual(sorted(p.name for p in outside.iterdir()), ["sentinel"])

    def test_diagnostic_refusal_is_a_cli_hard_failure(self):
        write_suite(self.root, "ok.test.py", "print('PASS')")
        output = self.root / "diagnostics"
        output.mkdir()
        victim = self.root / "sentinel.txt"
        victim.write_text("unchanged")
        (output / "index.tsv").symlink_to(victim)
        result, _ = self.run_fixture([], extra_args=["--diagnostics-dir", str(output)])
        self.assertEqual(result.returncode, 1)
        self.assertIn("FATAL: diagnostics write failed", result.stderr)
        self.assertIn("__diagnostics_write__::- verdict=ERROR kind=internal", result.stdout)
        self.assertEqual(victim.read_text(), "unchanged")


class CiLikeProfileFixture(unittest.TestCase):
    """P2: ci-like is a layer on top of build_isolated_env(), and the
    equals-form --isolation flag must be honored as an explicit override."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.mod = load_runner_module()

    def tearDown(self):
        self.tmp.cleanup()

    def test_ci_like_env_is_isolated_env_plus_a_layer(self):
        isolated_env = self.mod.build_isolated_env(self.root / "base")
        ci_like_env = self.mod.build_ci_like_env(self.root / "cilike", self.root)
        # every key build_isolated_env sets is still present (layered, not replaced)
        for key in isolated_env:
            if key == "PATH":
                continue  # ci-like pins its own PATH deliberately
            self.assertIn(key, ci_like_env)
        self.assertEqual(
            ci_like_env["PATH"], "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
        )
        self.assertEqual(ci_like_env["HEARTING_ENV_LAYOUT"], "github-runner")
        self.assertTrue(Path(ci_like_env["RUNNER_TEMP"]).is_dir())
        gitconfig = Path(ci_like_env["GIT_CONFIG_GLOBAL"])
        self.assertTrue(gitconfig.is_file())
        self.assertIn(str(self.root), gitconfig.read_text(encoding="utf-8"))

    def test_isolated_env_trusts_only_the_checkout_under_test(self):
        # A fresh HOME has no safe.directory, so git refused a checkout owned
        # by another uid and git-using suites skipped locally (2026-09-26).
        repo = self.root / "repo"
        repo.mkdir()
        env = self.mod.build_isolated_env(self.root / "iso", repo)
        gitconfig = Path(env["GIT_CONFIG_GLOBAL"])
        self.assertEqual(gitconfig.read_text(encoding="utf-8"), f"[safe]\n\tdirectory = {repo}\n")
        self.assertTrue(str(gitconfig).startswith(str(self.root / "iso")))
        default_env = self.mod.build_isolated_env(self.root / "default")
        self.assertIn(str(self.mod.ROOT), Path(default_env["GIT_CONFIG_GLOBAL"]).read_text(encoding="utf-8"))
        subprocess.run(["git", "init", "-q", str(repo)], check=True, env=env)
        listed = subprocess.run(["git", "config", "--global", "--get-all", "safe.directory"],
                                capture_output=True, text=True, env=env)
        self.assertEqual(listed.stdout.strip(), str(repo))

    def test_isolated_env_status_skips_the_optional_index_lock(self):
        # A suite-end SIGKILL during a refreshing `git status` left a holderless
        # index.lock on the real worktree (2026-10-21).
        repo = self.root / "repo"
        repo.mkdir()
        env = self.mod.build_isolated_env(self.root / "iso", repo)
        self.assertEqual(env["GIT_OPTIONAL_LOCKS"], "0")
        self.assertEqual(self.mod.build_ci_like_env(self.root / "ci", repo)["GIT_OPTIONAL_LOCKS"], "0")
        git = ["git", "-C", str(repo), "-c", "user.name=Fixture", "-c", "user.email=f@example.invalid"]
        (repo / "a.txt").write_text("a\n", encoding="utf-8")
        for args in (["init", "-q"], ["add", "a.txt"], ["commit", "-q", "-m", "a"]):
            subprocess.run(git + args, check=True, capture_output=True, env=env)
        os.utime(repo / "a.txt", (1_000_000_000, 1_000_000_000))
        index = repo / ".git" / "index"
        before = index.stat()
        subprocess.run(git + ["status", "--porcelain"], check=True, capture_output=True, env=env)
        after = index.stat()
        self.assertEqual((after.st_ino, after.st_mtime_ns), (before.st_ino, before.st_mtime_ns))

    def test_isolated_env_puts_the_checkout_under_test_on_the_import_path(self):
        repo = self.root / "repo"
        env = self.mod.build_isolated_env(self.root / "iso", repo)
        self.assertEqual(env["PYTHONPATH"], str(repo))

    def test_isolated_env_switches_the_workflow_group_review_off(self):
        # Sealing a cycle in a suite must not start a background model review.
        for env in (self.mod.build_isolated_env(self.root / "iso"),
                    self.mod.build_ci_like_env(self.root / "ci", self.root)):
            self.assertEqual(env["HEARTING_WORKFLOW_GROUP_REVIEW"], "off")
            self.assertEqual(env["HEARTING_CAMPAIGN_TITLE_AUTO"], "off")

    def test_ci_like_reuses_the_isolated_trust_entry(self):
        env = self.mod.build_ci_like_env(self.root / "ci", self.root)
        self.assertEqual(Path(env["GIT_CONFIG_GLOBAL"]).read_text(encoding="utf-8"),
                         f"[safe]\n\tdirectory = {self.root}\n")

    def test_equals_form_isolation_flag_overrides_declared_needs(self):
        write_suite(self.root, "needs_installed.test.py", "import sys\nsys.exit(0)\n")
        baseline = write_baseline(self.root, [])
        isolation = write_isolation_tsv(
            self.root,
            [f"needs_installed.test.py\tinstalled-layout\tr\tMA-TEST-016\t{TOMORROW}"],
        )
        report_path = self.root / "report.tsv"
        result = run_runner(
            [
                "--root", str(self.root),
                "--baseline", str(baseline),
                "--isolation-tsv", str(isolation),
                "--isolation=ci-like",
                "--jobs", "1",
                "--timeout", "5",
                "--no-leak-sweep",
                "--report", str(report_path),
                "--select", "needs_installed.test.py",
            ]
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("not a supported local pre-CI gate", result.stderr)
        rows = []
        lines = report_path.read_text(encoding="utf-8").splitlines()
        header = None
        for line in lines:
            if not line or line.startswith("#"):
                continue
            cols = line.split("\t")
            if header is None:
                header = cols
                continue
            rows.append(dict(zip(header, cols)))
        profiles = {r["isolation_profile"] for r in rows if r["suite_path"] == "needs_installed.test.py"}
        self.assertEqual(profiles, {"ci-like"})


class ExecutionPolicyFixture(unittest.TestCase):
    """The real full corpus stays within the measured retry/capacity envelope."""

    def setUp(self):
        self.mod = load_runner_module()

    def args(self, *extra):
        return self.mod.build_arg_parser().parse_args(list(extra))

    def test_full_repository_requires_retry_and_caps_jobs(self):
        errors = self.mod.execution_policy_errors(
            self.args("--jobs", "5"), self.mod.ROOT.resolve()
        )
        self.assertIn("full repository runs require --retries 1 or greater", errors)
        self.assertTrue(any("--jobs <= 4" in error for error in errors), errors)

    def test_selected_run_keeps_narrow_diagnostic_freedom(self):
        errors = self.mod.execution_policy_errors(
            self.args("--select", "utilities/example.test.py", "--jobs", "8"),
            self.mod.ROOT.resolve(),
        )
        self.assertEqual(errors, [])

    def test_ci_like_refuses_an_unnarrowed_run(self):
        errors = self.mod.execution_policy_errors(
            self.args("--isolation=ci-like", "--retries", "1"),
            self.mod.ROOT.resolve(),
        )
        self.assertTrue(any("diagnostic-only" in error for error in errors), errors)

    def test_invalid_counts_are_rejected_before_any_suite_runs(self):
        errors = self.mod.execution_policy_errors(
            self.args("--select", "x", "--jobs", "0", "--retries", "-1"),
            self.mod.ROOT.resolve(),
        )
        self.assertIn("--jobs must be positive", errors)
        self.assertIn("--retries must be non-negative", errors)


class RetryBudgetFixture(RunTestsFixtureBase):
    """P3: the serial retry pass is bounded in total, and the per-suite
    timeout still applies to every individual attempt."""

    def write_slow_flaky_suite(self, sleep_s: float):
        write_suite(self.root, "slow_flaky.test.py", f"""
            from pathlib import Path
            import sys, time
            p = Path('attempts.txt')
            n = int(p.read_text()) + 1 if p.exists() else 1
            p.write_text(str(n))
            time.sleep({sleep_s})
            sys.exit(1)
        """)

    def write_never_ending_suite(self):
        write_suite(self.root, "slow_flaky.test.py", """
            from pathlib import Path
            import time
            p = Path('attempts.txt')
            n = int(p.read_text()) + 1 if p.exists() else 1
            p.write_text(str(n))
            time.sleep(60)
        """)

    def baseline(self):
        return [
            "slow_flaky.test.py\t-\texit-nonzero\tisolated\t"
            f"flaky-timing: intermittent\tMA-RETRY-BUDGET\t{TOMORROW}"
        ]

    def baseline_timeout(self):
        return [
            "slow_flaky.test.py\t-\ttimeout\tisolated\t"
            f"flaky-timing: intermittent\tMA-RETRY-BUDGET\t{TOMORROW}"
        ]

    def test_exhausted_budget_is_recorded_and_skips_remaining_retries(self):
        # A suite that always times out keeps every attempt's kind identical
        # (timeout), so the budget running out (remaining <= 0) is the only
        # variable -- a mixed-kind fixture would make an ordinary attempt
        # timeout race with the exhaustion check.
        self.write_never_ending_suite()
        result, rows = self.run_fixture(
            self.baseline_timeout(),
            extra_args=["--retries", "3", "--retry-budget", "2", "--timeout", "1"],
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("retry-budget-exhausted", rows[0]["detail"])
        self.assertEqual(rows[0]["verdict"], "KNOWN-FAIL")
        # budget (2s / 1s-clamped attempts) stops the pass after 2 retries,
        # not all 3 requested; plus the one initial (non-retry) attempt.
        attempts = int((self.root / "attempts.txt").read_text())
        self.assertLess(attempts, 4)

    def test_budget_within_reach_is_not_recorded(self):
        self.write_slow_flaky_suite(0.0)
        result, rows = self.run_fixture(
            self.baseline(), extra_args=["--retries", "1", "--retry-budget", "60"]
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("retry-budget-exhausted", rows[0]["detail"])
        self.assertIn("attempts=2", rows[0]["detail"])

    def test_all_pass_under_sub_second_clamps_is_flaky_pass_with_no_exhausted_label(self):
        # Owner-observed regression input: utilities/dispatch_progress.test.py
        # ran attempts=3, outcomes=pass,pass,pass under a tight retry budget,
        # and the pre-fix runner wrongly tagged that all-pass aggregate with
        # retry-budget-exhausted (a positive, non-exhausting clamp mislabeled
        # as exhaustion) and would have reported it as XPASS. Neither defect
        # may resurface: this is FLAKY-PASS with no exhausted label, even
        # though every attempt after the first ran under a sub-second clamp.
        write_suite(self.root, "slow_flaky.test.py", """
            from pathlib import Path
            import sys, time
            p = Path('attempts.txt')
            n = int(p.read_text()) + 1 if p.exists() else 1
            p.write_text(str(n))
            time.sleep(0.05)
        """)
        result, rows = self.run_fixture(
            self.baseline(),
            extra_args=["--retries", "3", "--retry-budget", "1", "--timeout", "5"],
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(rows[0]["verdict"], "FLAKY-PASS")
        self.assertIn("attempts=4", rows[0]["detail"])
        self.assertNotIn("retry-budget-exhausted", rows[0]["detail"])

    def test_flaky_aggregate_preserves_failing_test_ids(self):
        write_suite(self.root, "named_flaky.test.py", """
            import unittest
            class T(unittest.TestCase):
                def test_specific_thing(self):
                    self.fail('boom')
            unittest.main()
        """)
        result, rows = self.run_fixture(
            [
                "named_flaky.test.py\t-\tassertion\tisolated\t"
                f"flaky-timing: intermittent\tMA-RETRY-IDS\t{TOMORROW}"
            ],
            extra_args=["--retries", "1"],
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        # the whole-file aggregate row still names which test failed
        self.assertIn("failing=T.test_specific_thing", rows[0]["detail"])


class ForeignFlakyRetryFixture(RunTestsFixtureBase):
    """impl-review round 1 finding 1: the retry aggregation branch computes its
    verdict directly instead of going through classify_result(), so it must
    apply the same fingerprint filter or it becomes a way around P8."""

    def test_foreign_flaky_row_cannot_make_a_failing_suite_known_fail(self):
        write_suite(self.root, "flaky.test.py", "import sys\nsys.exit(1)\n")
        rows = [
            "flaky.test.py\t-\texit-nonzero\tisolated\t"
            f"flaky-timing: intermittent\tMA-FP-RETRY\t{TOMORROW}\tforeign-host"
        ]
        result, report_rows = self.run_fixture(rows, extra_args=["--retries", "1"])
        verdicts = [r["verdict"] for r in report_rows]
        self.assertNotIn("KNOWN-FAIL", verdicts, report_rows)
        self.assertNotIn("FLAKY-KNOWN-FAIL", verdicts, report_rows)
        self.assertNotIn("BASELINE-FOREIGN", verdicts, report_rows)
        self.assertEqual(verdicts, ["FAIL"], report_rows)
        self.assertNotEqual(result.returncode, 0)


class SeedBaselineGuardFixture(RunTestsFixtureBase):
    """impl-review round 1 finding 2: a proposal must not be able to become the
    contract by being written over it."""

    def test_seeding_onto_the_configured_baseline_is_refused(self):
        write_suite(self.root, "fails.test.py", "import sys\nsys.exit(1)\n")
        baseline = write_baseline(self.root, [
            f"other.test.py\t-\tassertion\tisolated\tfixture\tMA-KEEP\t{TOMORROW}\t",
        ])
        isolation = write_isolation_tsv(self.root, None)
        before = baseline.read_bytes()
        result = run_runner([
            "--root", str(self.root), "--baseline", str(baseline),
            "--isolation-tsv", str(isolation), "--isolation=isolated",
            "--jobs", "1", "--timeout", "5", "--no-leak-sweep",
            "--seed-baseline", str(baseline),
        ])
        self.assertEqual(result.returncode, 64, result.stdout + result.stderr)
        self.assertEqual(baseline.read_bytes(), before)

    def test_seeding_onto_a_hardlink_alias_of_the_baseline_is_refused(self):
        # A differently-named hardlink to the same inode is the same file:
        # plain resolve()-equality misses this, samefile() does not.
        write_suite(self.root, "fails.test.py", "import sys\nsys.exit(1)\n")
        baseline = write_baseline(self.root, [
            f"other.test.py\t-\tassertion\tisolated\tfixture\tMA-KEEP\t{TOMORROW}\t",
        ])
        isolation = write_isolation_tsv(self.root, None)
        alias = self.root / "baseline-alias.tsv"
        os.link(baseline, alias)
        before = baseline.read_bytes()
        result = run_runner([
            "--root", str(self.root), "--baseline", str(baseline),
            "--isolation-tsv", str(isolation), "--isolation=isolated",
            "--jobs", "1", "--timeout", "5", "--no-leak-sweep",
            "--seed-baseline", str(alias),
        ])
        self.assertEqual(result.returncode, 64, result.stdout + result.stderr)
        self.assertEqual(baseline.read_bytes(), before)


class RetryBudgetBoundFixture(RunTestsFixtureBase):
    """impl-review round 1 finding 3: checking the budget only before an attempt
    does not bound that attempt."""

    def test_a_retry_attempt_cannot_outrun_the_remaining_budget(self):
        write_suite(self.root, "slow.test.py", """
            import time
            time.sleep(60)
        """)
        rows = [
            "slow.test.py\t-\ttimeout\tisolated\t"
            f"flaky-timing: intermittent\tMA-BUDGET\t{TOMORROW}\t"
        ]
        started = time.time()
        result, report_rows = self.run_fixture(
            rows, extra_args=["--retries", "1", "--timeout", "20", "--retry-budget", "3"],
        )
        elapsed = time.time() - started
        # The main pass costs the full 20s timeout; the retry must be clamped to
        # the ~3s of budget left rather than running another 20s.
        self.assertLess(elapsed, 35, f"{elapsed:.1f}s\n{result.stdout}")
        # Only one retry was requested and it ran (clamped to the ~3s left):
        # the budget was never driven to <= 0, so this is an ordinary
        # positive clamp, not exhaustion, and must not carry the label.
        self.assertNotIn("retry-budget-exhausted", report_rows[0]["detail"])


class TimeoutDescendantReclaimFixture(RunTestsFixtureBase):
    """P3: a timed-out suite is killed together with the descendants it left
    behind, so a supervisor or fake-server daemon cannot outlive the runner."""

    def test_grandchild_is_reaped_when_the_suite_times_out(self):
        pidfile = self.root / "grandchild.pid"
        write_suite(self.root, "leaky.test.py", f"""
            import subprocess, sys, time
            # a grandchild that would outlive its parent
            child = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(120)"]
            )
            open({str(pidfile)!r}, "w").write(str(child.pid))
            time.sleep(120)
        """)
        result, rows = self.run_fixture(
            [], extra_args=["--timeout", "2"]
        )
        self.assertEqual(rows[0]["verdict"], "FAIL")
        self.assertEqual(rows[0]["kind"], "timeout")
        grandchild_pid = int(pidfile.read_text())
        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                os.kill(grandchild_pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.2)
        else:
            os.kill(grandchild_pid, 9)
            self.fail(f"grandchild {grandchild_pid} survived the suite timeout")
        _ = result

    def test_setsid_fixture_is_reaped_before_its_temp_root_is_removed(self):
        pidfile = self.root / "detached.pid"
        write_suite(self.root, "detached.test.py", f"""
            import os, subprocess, sys
            child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'],
                cwd=os.environ['TMPDIR'], start_new_session=True, stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            open({str(pidfile)!r}, 'w').write(str(child.pid))
        """)
        result, rows = self.run_fixture([], extra_args=["--timeout", "10"])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(rows[0]["verdict"], "PASS")
        pid = int(pidfile.read_text())
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            try:
                fields = Path(f"/proc/{pid}/stat").read_bytes().rsplit(b")", 1)[1].split()
            except (OSError, IndexError):
                break
            if fields[0] == b"Z":
                break
            time.sleep(.02)
        else:
            self.fail(f"setsid fixture {pid} survived suite temp-root cleanup")

    def test_reaper_spares_an_unmarked_process_in_the_same_group(self):
        pidfile, bystander_file = self.root / "marked.pid", self.root / "bystander.pid"
        write_suite(self.root, "grouped.test.py", f"""
            import os, subprocess, sys, time
            spawn_bystander = (
                "import subprocess, sys, time; "
                "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'], env={{}}, "
                "cwd='/', stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL); "
                "open(sys.argv[1], 'w').write(str(p.pid)); time.sleep(120)")
            child = subprocess.Popen([sys.executable, '-c', spawn_bystander, {str(bystander_file)!r}],
                cwd=os.environ['TMPDIR'], start_new_session=True, stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            open({str(pidfile)!r}, 'w').write(str(child.pid))
            deadline = time.monotonic() + 10
            while not os.path.exists({str(bystander_file)!r}) and time.monotonic() < deadline:
                time.sleep(.02)
        """)
        result, rows = self.run_fixture([], extra_args=["--timeout", "20"])
        bystander = int(bystander_file.read_text())
        self.addCleanup(lambda: os.kill(bystander, signal.SIGKILL) if Path(f"/proc/{bystander}").exists() else None)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(rows[0]["verdict"], "PASS")
        state = Path(f"/proc/{bystander}/stat").read_bytes().rsplit(b")", 1)[1].split()[0]
        self.assertNotEqual(state, b"Z", "an unmarked process in the marked group was killed")


class FingerprintFixture(unittest.TestCase):
    """P8/MA-W1-011: the fingerprint decides whether a baseline row may be
    applied. It is never a verdict that can preempt a failure."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.mod = load_runner_module()

    def tearDown(self):
        self.tmp.cleanup()

    def test_fingerprint_is_deterministic_for_one_environment(self):
        env = self.mod.build_isolated_env(self.root / "a")
        first = self.mod.environment_fingerprint(env, "isolated")
        second = self.mod.environment_fingerprint(env, "isolated")
        self.assertEqual(first, second)
        self.assertEqual(len(first), 16)

    def test_profiles_with_different_environments_differ(self):
        isolated = self.mod.environment_fingerprint(
            self.mod.build_isolated_env(self.root / "iso"), "isolated"
        )
        ci_like = self.mod.environment_fingerprint(
            self.mod.build_ci_like_env(self.root / "ci", self.root), "ci-like"
        )
        self.assertNotEqual(isolated, ci_like)

    def test_empty_fingerprint_is_unknown_and_keeps_the_old_contract(self):
        self.assertTrue(self.mod.baseline_row_applicable({"fingerprint": ""}, "abc123"))
        self.assertTrue(self.mod.baseline_row_applicable({}, "abc123"))
        self.assertTrue(self.mod.baseline_row_applicable({"fingerprint": "abc123"}, "abc123"))
        self.assertFalse(self.mod.baseline_row_applicable({"fingerprint": "other"}, "abc123"))

    # --- the six-row verdict table (plan P8) -------------------------------

    def _baseline(self, *, kind="assertion", review_by=None, fingerprint="host-a", test_id="-"):
        review_by = review_by or TOMORROW
        return {
            ("s.test.py", test_id): {
                "suite_path": "s.test.py", "test_id": test_id,
                "expected_failure_kind": kind, "isolation_profile": "isolated",
                "reason": "fixture", "defect_id": "MA-FP", "review_by": review_by,
                "fingerprint": fingerprint,
            }
        }

    def _result(self, passed=True):
        mod = self.mod
        stderr = "" if passed else "AssertionError: boom\nFAILED (failures=1)\n"
        return mod.SuiteResult("s.test.py", 0 if passed else 1, False, "", stderr, 0.1, "isolated")

    def test_row1_foreign_pass_is_held_not_reported_as_xpass(self):
        verdicts = self.mod.classify_result(
            self._result(passed=True), self._baseline(), TODAY, "host-b"
        )
        self.assertEqual([v.verdict for v in verdicts], ["BASELINE-FOREIGN"])
        self.assertNotIn("BASELINE-FOREIGN", self.mod.HARD_FAIL_VERDICTS)

    def test_row2_unlisted_failure_is_unchanged_by_the_fingerprint(self):
        verdicts = self.mod.classify_result(self._result(passed=False), {}, TODAY, "host-b")
        self.assertEqual([v.verdict for v in verdicts], ["FAIL"])
        self.assertNotIn("baseline-foreign", verdicts[0].detail)

    def test_row3_foreign_expired_row_stays_a_hard_failure(self):
        verdicts = self.mod.classify_result(
            self._result(passed=False),
            self._baseline(review_by=YESTERDAY), TODAY, "host-b",
        )
        self.assertEqual([v.verdict for v in verdicts], ["FAIL"])
        self.assertIn("baseline-foreign", verdicts[0].detail)
        self.assertIn(verdicts[0].verdict, self.mod.HARD_FAIL_VERDICTS)

    def test_row4_foreign_kind_mismatch_row_stays_a_hard_failure(self):
        verdicts = self.mod.classify_result(
            self._result(passed=False), self._baseline(kind="timeout"), TODAY, "host-b",
        )
        self.assertEqual([v.verdict for v in verdicts], ["FAIL"])
        self.assertIn("baseline-foreign", verdicts[0].detail)
        self.assertIn(verdicts[0].verdict, self.mod.HARD_FAIL_VERDICTS)

    def test_row5_stale_is_a_corpus_judgement_and_ignores_the_fingerprint(self):
        # STALE says "this baseline row points at a suite that no longer
        # exists", which is true regardless of where the row was recorded.
        # Exempting it by fingerprint would keep dead rows alive forever.
        write_suite(self.root, "live.test.py", "import sys\nsys.exit(0)\n")
        baseline = write_baseline(self.root, [
            f"gone.test.py\t-\tassertion\tisolated\tfixture\tMA-FP\t{TOMORROW}\thost-a",
        ])
        isolation = write_isolation_tsv(self.root, None)
        report = self.root / "r.tsv"
        result = run_runner([
            "--root", str(self.root), "--baseline", str(baseline),
            "--isolation-tsv", str(isolation), "--isolation=isolated",
            "--jobs", "1", "--timeout", "5", "--no-leak-sweep",
            "--report", str(report),
        ])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("STALE=1", result.stdout)

    def test_row6_foreign_known_fail_becomes_a_hard_failure(self):
        # The most important row: under the rejected preempt-first design this
        # silently became a non-hard-fail, so a genuine CI assertion failure
        # would have disappeared behind the fingerprint.
        verdicts = self.mod.classify_result(
            self._result(passed=False), self._baseline(), TODAY, "host-b",
        )
        self.assertEqual([v.verdict for v in verdicts], ["FAIL"])
        self.assertIn(verdicts[0].verdict, self.mod.HARD_FAIL_VERDICTS)
        # and with a matching fingerprint the row still applies normally
        matching = self.mod.classify_result(
            self._result(passed=False), self._baseline(), TODAY, "host-a",
        )
        self.assertEqual([v.verdict for v in matching], ["KNOWN-FAIL"])

    def test_invariant_a_failing_run_never_yields_baseline_foreign(self):
        # The mechanical definition of "the fingerprint cannot turn a failure
        # green": no failing execution, under any baseline combination, may
        # produce BASELINE-FOREIGN.
        combos = [
            {},
            self._baseline(),
            self._baseline(kind="timeout"),
            self._baseline(review_by=YESTERDAY),
            self._baseline(fingerprint=""),
            self._baseline(test_id="T.test_a"),
            {**self._baseline(), **self._baseline(test_id="T.test_a")},
        ]
        for fingerprint in ("host-a", "host-b", ""):
            for baseline in combos:
                verdicts = self.mod.classify_result(
                    self._result(passed=False), baseline, TODAY, fingerprint
                )
                self.assertTrue(
                    all(v.verdict != "BASELINE-FOREIGN" for v in verdicts),
                    (fingerprint, baseline, [v.verdict for v in verdicts]),
                )

    def test_seed_baseline_writes_a_proposal_stamped_with_the_fingerprint(self):
        write_suite(self.root, "fails.test.py", "import sys\nsys.exit(1)\n")
        baseline = write_baseline(self.root, [])
        isolation = write_isolation_tsv(self.root, None)
        seed = self.root / "seed.tsv"
        result = run_runner([
            "--root", str(self.root), "--baseline", str(baseline),
            "--isolation-tsv", str(isolation), "--isolation=isolated",
            "--jobs", "1", "--timeout", "5", "--no-leak-sweep",
            "--seed-baseline", str(seed),
        ])
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(seed.is_file())
        lines = [l for l in seed.read_text().splitlines() if l and not l.startswith("#")]
        header, *rows = lines
        self.assertEqual(header.split("\t"), self.mod.BASELINE_COLUMNS)
        self.assertTrue(rows, seed.read_text())
        stamped = dict(zip(self.mod.BASELINE_COLUMNS, rows[0].split("\t")))
        self.assertEqual(stamped["suite_path"], "fails.test.py")
        self.assertEqual(len(stamped["fingerprint"]), 16)
        # the repository baseline is never rewritten by seeding
        self.assertEqual(baseline.read_text().count("fails.test.py"), 0)


class UserSiteYamlBridge(unittest.TestCase):
    """Only an enabled user-site PyYAML package crosses suite isolation."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.mod = load_runner_module()

    def tearDown(self):
        self.tmp.cleanup()

    def _assert_isolated_child_import(self, companion_shape):
        parent_site = self.root / f"parent-site-{companion_shape}"
        package = parent_site / "yaml"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text(
            "SOURCE = 'parent-user-yaml'\n", encoding="utf-8"
        )
        companion_source = f"optional-{companion_shape}"
        if companion_shape == "package":
            companion = parent_site / "_yaml"
            companion.mkdir()
            (companion / "__init__.py").write_text(
                f"SOURCE = {companion_source!r}\n", encoding="utf-8"
            )
        else:
            companion = parent_site / "_yaml.py"
            companion.write_text(
                f"SOURCE = {companion_source!r}\n", encoding="utf-8"
            )
        (parent_site / "sibling_only.py").write_text("VALUE = 1\n", encoding="utf-8")
        pth_marker = self.root / "pth-executed"
        (parent_site / "parent.pth").write_text(
            f"import pathlib; pathlib.Path({str(pth_marker)!r}).write_text('ran')\n",
            encoding="utf-8",
        )
        (parent_site / "sitecustomize.py").write_text(
            f"from pathlib import Path; Path({str(self.root / 'sitecustomize-ran')!r}).touch()\n",
            encoding="utf-8",
        )
        (parent_site / "usercustomize.py").write_text(
            f"from pathlib import Path; Path({str(self.root / 'usercustomize-ran')!r}).touch()\n",
            encoding="utf-8",
        )

        suite_tmp = self.root / f"suite-{companion_shape}"
        parent_environment = os.environ.copy()
        with mock.patch.object(self.mod.site, "ENABLE_USER_SITE", True), \
             mock.patch.object(self.mod.site, "getusersitepackages", return_value=str(parent_site)):
            env = self.mod.build_isolated_env(suite_tmp)
        self.assertEqual(os.environ, parent_environment)

        private_site = self.mod._child_user_site(suite_tmp / "home")
        self.assertTrue((private_site / "yaml").is_symlink())
        self.assertFalse((private_site / "sibling_only.py").exists())
        self.assertFalse((private_site / "sitecustomize.py").exists())
        self.assertFalse(any(private_site.glob("parent*.pth")))
        if companion_shape == "package":
            self.assertTrue((private_site / "_yaml").is_symlink())
            self.assertEqual((private_site / "_yaml").resolve(), companion.resolve())
        else:
            self.assertTrue((private_site / "_yaml.py").is_symlink())
            self.assertEqual((private_site / "_yaml.py").resolve(), companion.resolve())
        result = subprocess.run(
            [sys.executable, "-c", textwrap.dedent("""
                import json
                import sys
                import yaml
                import _yaml
                try:
                    import sibling_only
                    sibling_visible = True
                except ImportError:
                    sibling_visible = False
                print(json.dumps({"yaml_source": yaml.SOURCE,
                                  "yaml_file": yaml.__file__,
                                  "companion_source": _yaml.SOURCE,
                                  "companion_file": _yaml.__file__,
                                  "sibling": sibling_visible,
                                  "parent_site_on_path": sys.path.count(%r) > 0}))
            """ % str(parent_site))],
            env=env, capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        imported = json.loads(result.stdout)
        self.assertEqual(imported["yaml_source"], "parent-user-yaml")
        self.assertEqual(imported["companion_source"], companion_source)
        self.assertFalse(imported["sibling"])
        self.assertFalse(imported["parent_site_on_path"])
        self.assertTrue(imported["yaml_file"].startswith(str(private_site / "yaml")))
        self.assertTrue(imported["companion_file"].startswith(str(private_site / "_yaml")))
        self.assertFalse(pth_marker.exists())
        self.assertFalse((self.root / "sitecustomize-ran").exists())
        self.assertFalse((self.root / "usercustomize-ran").exists())

    def test_yaml_is_visible_without_parent_siblings_or_startup_hooks(self):
        self._assert_isolated_child_import("module")

    def test_package_shaped_yaml_companion_is_importable_in_isolated_child(self):
        self._assert_isolated_child_import("package")

    def test_disabled_or_missing_user_yaml_is_not_linked(self):
        parent_site = self.root / "parent-site"
        package = parent_site / "yaml"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text("SOURCE = 'user'\n", encoding="utf-8")
        with mock.patch.object(self.mod.site, "ENABLE_USER_SITE", False), \
             mock.patch.object(self.mod.site, "getusersitepackages", return_value=str(parent_site)):
            disabled = self.mod.build_isolated_env(self.root / "disabled")
        disabled_site = self.mod._child_user_site(Path(disabled["HOME"]))
        self.assertFalse((disabled_site / "yaml").exists())

        missing_site = self.root / "missing-site"
        missing_site.mkdir()
        with mock.patch.object(self.mod.site, "ENABLE_USER_SITE", True), \
             mock.patch.object(self.mod.site, "getusersitepackages", return_value=str(missing_site)):
            missing = self.mod.build_isolated_env(self.root / "missing")
        missing_private_site = self.mod._child_user_site(Path(missing["HOME"]))
        self.assertFalse((missing_private_site / "yaml").exists())


class PerSuiteProgressEvents(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.mod = load_runner_module()

    def tearDown(self):
        self.tmp.cleanup()

    def test_pass_fail_and_timeout_events_include_suite_profile_result_and_duration(self):
        cases = (
            ("pass.test.py", "print('passed')\n", 5, "pass", "suite_end"),
            ("fail.test.py", "raise SystemExit(3)\n", 5, "fail", "suite_end"),
            ("timeout.test.py", "import time\ntime.sleep(30)\n", 1, "timeout", "suite_timeout"),
        )
        for relpath, body, timeout, expected, final_event in cases:
            with self.subTest(suite=relpath):
                suite = write_suite(self.root, relpath, body)
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    results = self.mod.run_profile(
                        [suite], self.root, "isolated", 1, timeout, None
                    )
                events = [json.loads(line) for line in output.getvalue().splitlines()]
                self.assertEqual([event["event"] for event in events], [
                    "suite_prepare_start", "suite_prepare_end", "suite_start", final_event,
                ])
                self.assertTrue(all(event["suite"] == relpath for event in events))
                self.assertTrue(all(event["profile"] == "isolated" for event in events))
                self.assertEqual(events[1]["result"], "ready")
                self.assertEqual(events[-1]["result"], expected)
                self.assertIsInstance(events[-1]["duration_s"], (int, float))
                self.assertEqual(results[0].passed, expected == "pass")

    def test_start_line_is_visible_while_suite_subprocess_is_still_running(self):
        suite = write_suite(self.root, "slow.test.py", "import time\ntime.sleep(3)\n")
        driver = textwrap.dedent("""
            import runpy
            import sys
            from pathlib import Path
            runner = runpy.run_path(sys.argv[1])
            runner["run_profile"]([Path(sys.argv[2])], Path(sys.argv[3]),
                                  "isolated", 1, 10, None)
        """)
        process = subprocess.Popen(
            [sys.executable, "-c", driver, str(RUNNER), str(suite), str(self.root)],
            cwd=str(ROOT), env=CLEAN_ENV, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1,
        )
        observed = False
        try:
            assert process.stdout is not None
            for line in process.stdout:
                event = json.loads(line)
                if event["event"] == "suite_start":
                    observed = True
                    self.assertEqual(event["suite"], "slow.test.py")
                    self.assertIsNone(process.poll())
                    break
            _stdout, stderr = process.communicate(timeout=15)
            self.assertEqual(process.returncode, 0, stderr)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
        self.assertTrue(observed)


class TempParentProbeFixture(unittest.TestCase):
    """C-5: the Git containment probe must distinguish a real "not a
    repository" answer from any other probe failure."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.mod = load_runner_module()

    def tearDown(self):
        self.tmp.cleanup()

    def fake_git(self, name: str, body: str) -> Path:
        path = self.root / name
        path.write_text("#!/bin/sh\n" + textwrap.dedent(body), encoding="utf-8")
        path.chmod(0o755)
        return path

    def test_real_non_repository_is_accepted(self):
        git = self.fake_git("git-none", """
            echo 'fatal: not a git repository (or any of the parent directories): .git' >&2
            exit 128
        """)
        self.assertEqual(self.mod.probe_git_containment(str(git), self.root), "outside")

    def test_worktree_hit_is_inside(self):
        git = self.fake_git("git-inside", """
            echo /repo
            exit 0
        """)
        self.assertEqual(self.mod.probe_git_containment(str(git), self.root), "inside")

    def test_probe_error_is_uncertain_not_a_safe_candidate(self):
        for name, body in (
            ("git-perm", "echo 'fatal: could not read Error: Permission denied' >&2\nexit 128\n"),
            ("git-broken", "echo 'error: object file is empty' >&2\nexit 1\n"),
            ("git-silent", "exit 129\n"),
            ("git-empty-ok", "exit 0\n"),
        ):
            with self.subTest(name=name):
                git = self.fake_git(name, body)
                self.assertEqual(self.mod.probe_git_containment(str(git), self.root), "uncertain")

    def test_probe_timeout_is_uncertain_and_uses_ten_second_limit(self):
        with mock.patch.object(
            self.mod.subprocess, "run",
            side_effect=subprocess.TimeoutExpired(cmd="git", timeout=10),
        ) as probe:
            self.assertEqual(
                self.mod.probe_git_containment("git", self.root), "uncertain"
            )
        self.assertEqual(probe.call_args.kwargs["timeout"], 10)

    def test_missing_binary_is_uncertain(self):
        self.assertEqual(
            self.mod.probe_git_containment(str(self.root / "no-such-git"), self.root),
            "uncertain",
        )

    def test_non_standard_git_is_resolved_through_the_override(self):
        git = self.fake_git("git-elsewhere", "exit 128\n")
        with mock.patch.dict(os.environ, {"RUN_TESTS_GIT": str(git)}, clear=False):
            self.assertEqual(self.mod.resolve_git_executable(), str(git))
        with mock.patch.dict(os.environ, {"RUN_TESTS_GIT": str(self.root / "absent")}, clear=False):
            self.assertIsNone(self.mod.resolve_git_executable())

    def test_all_candidates_uncertain_exits_70(self):
        # every probe uncertain (incl. /var/tmp) -> refuse, never guess.
        git = self.fake_git("git-uncertain", "exit 129\n")
        with mock.patch.dict(os.environ, {"RUN_TESTS_GIT": str(git),
                                          "RUN_TESTS_TMP_ROOT": str(self.root)}, clear=False):
            with self.assertRaises(SystemExit) as caught:
                self.mod.choose_suite_temp_parent()
        self.assertEqual(caught.exception.code, 70)

    def test_unusable_candidate_is_skipped_before_the_probe(self):
        # a configured root that cannot be made writable (the "/var/tmp is
        # unavailable" shape) is rejected without being probed.
        blocked = self.root / "blocked"
        blocked.mkdir(mode=0o500)
        good = self.root / "good"
        good.mkdir()
        git = self.fake_git("git-skip", f"""
            case "$2" in
              {blocked}) echo 'probed a candidate that should have been skipped' >&2; exit 0 ;;
              {good}) echo 'fatal: not a git repository (or any of the parent directories): .git' >&2; exit 128 ;;
              *) exit 129 ;;
            esac
        """)
        with mock.patch.dict(os.environ, {"RUN_TESTS_GIT": str(git),
                                          "RUN_TESTS_TMP_ROOT": str(blocked)}, clear=False):
            with mock.patch.object(self.mod.tempfile, "gettempdir", return_value=str(good)):
                self.assertEqual(self.mod.choose_suite_temp_parent(), good)

    def test_no_git_executable_exits_70(self):
        with mock.patch.dict(os.environ, {"RUN_TESTS_GIT": str(self.root / "absent")}, clear=False):
            with self.assertRaises(SystemExit) as caught:
                self.mod.choose_suite_temp_parent()
        self.assertEqual(caught.exception.code, 70)

    def test_first_uncertain_candidate_falls_through_to_the_next(self):
        marker = self.root / "good"
        marker.mkdir()
        bad = self.root / "bad"
        bad.mkdir()
        git = self.fake_git("git-selective", f"""
            case "$2" in
              {marker}) echo 'fatal: not a git repository (or any of the parent directories): .git' >&2; exit 128 ;;
              *) exit 129 ;;
            esac
        """)
        with mock.patch.dict(os.environ, {"RUN_TESTS_GIT": str(git),
                                          "RUN_TESTS_TMP_ROOT": str(bad)}, clear=False):
            with mock.patch.object(self.mod.tempfile, "gettempdir", return_value=str(marker)):
                chosen = self.mod.choose_suite_temp_parent()
        self.assertEqual(chosen, marker)


class LiveStateLeakFixture(RunTestsFixtureBase):
    def test_leak_sweep_catches_a_suite_that_writes_to_live_state(self):
        # Point HOME at a throwaway dir so this test never touches the real
        # live state root, but exercise the sweep mechanism itself: a suite
        # that writes under $HOME/.local/state/hearting must be caught.
        fake_home = Path(tempfile.mkdtemp(prefix="w1-fake-home-"))
        (fake_home / ".local" / "state" / "hearting").mkdir(parents=True)
        # Hold the fd open across the sampler's poll interval so the bounded
        # /proc-based sampler (which observes open fds, not instantaneous
        # syscalls) has a real chance to attribute the write to this suite's
        # own process-group lineage.
        write_suite(
            self.root,
            "leaky.test.py",
            f"""
            from pathlib import Path
            import time
            p = Path({str(fake_home / '.local' / 'state' / 'hearting')!r}) / "leak-marker.json"
            with open(p, "w") as f:
                f.write("{{}}")
                f.flush()
                time.sleep(0.8)
            """,
        )
        baseline = write_baseline(self.root, [])
        isolation = write_isolation_tsv(self.root)
        report_path = self.root / "leak-report.tsv"
        env = dict(os.environ)
        env["HOME"] = str(fake_home)
        result = subprocess.run(
            [
                sys.executable, str(RUNNER),
                "--root", str(self.root),
                "--baseline", str(baseline),
                "--isolation-tsv", str(isolation),
                "--isolation=isolated",
                "--report", str(report_path),
            ],
            cwd=str(ROOT),
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("LIVE-STATE-LEAK", result.stderr)

    def test_foreign_concurrent_write_is_reported_but_not_fatal(self):
        # A write under live-state that no suite's own process-group lineage
        # produced (simulated here by writing it from the test process itself,
        # standing in for a concurrent session) must surface as an advisory,
        # never as the fatal __live_state_leak__ verdict. The whole-run
        # foreign sampler can only positively attribute a write to a PGID
        # this run never spawned while that PGID still holds the fd open, so
        # the writer here holds it open across the sampler's poll interval --
        # mirroring how LiveStateLeakFixture proves *own* attribution above.
        fake_home = Path(tempfile.mkdtemp(prefix="w1-fake-home-foreign-"))
        live_dir = fake_home / ".local" / "state" / "hearting"
        live_dir.mkdir(parents=True)
        # Signal from the suite process after the nested runner has taken its
        # before-snapshot and started the whole-run fd sampler. A startup sleep
        # in the parent made this test depend on how quickly Python imported
        # the runner under host load.
        ready = self.root / "suite-ready"
        write_suite(
            self.root, "harmless.test.py",
            f"from pathlib import Path\nimport time\n"
            f"Path({str(ready)!r}).write_text('ready')\ntime.sleep(1.5)\n",
        )
        baseline = write_baseline(self.root, [])
        isolation = write_isolation_tsv(self.root)
        report_path = self.root / "leak-report.tsv"
        env = dict(os.environ)
        env["HOME"] = str(fake_home)

        def write_foreign_entry_mid_run():
            deadline = time.time() + 30
            while not ready.exists() and time.time() < deadline:
                time.sleep(0.01)
            if not ready.exists():
                return
            with open(live_dir / "foreign-marker.json", "w") as f:
                f.write("{}")
                f.flush()
                time.sleep(1.0)

        import threading as _threading
        writer = _threading.Thread(target=write_foreign_entry_mid_run, daemon=True)
        writer.start()
        try:
            result = subprocess.run(
                [
                    sys.executable, str(RUNNER),
                    "--root", str(self.root),
                    "--baseline", str(baseline),
                    "--isolation-tsv", str(isolation),
                    "--isolation=isolated",
                    "--report", str(report_path),
                ],
                cwd=str(ROOT),
                env=env,
                capture_output=True,
                text=True,
                timeout=60,
            )
        finally:
            writer.join(timeout=5)
        self.assertTrue(ready.exists(), "fixture suite never entered the runner's live-state sampling window")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("LIVE-STATE-OBSERVED-FOREIGN", result.stderr)
        self.assertNotIn("LIVE-STATE-LEAK:", result.stderr)
        self.assertNotIn("LIVE-STATE-UNATTRIBUTED", result.stderr)

    # ------------------------------------------------------------------
    # Fast open/write/close: neither sampler can observe an fd that is gone
    # before the next 0.2s poll, so such a write is UNATTRIBUTED whoever made
    # it. The contract is that absence of evidence is advisory by default and
    # fatal only in the single-tenant --strict-leak-sweep mode; these
    # regressions must hold no matter when the write lands relative to the
    # samplers, so they never assert on timing.
    # ------------------------------------------------------------------

    def _leak_sweep_run(self, fake_home: Path, *extra_args: str):
        baseline = write_baseline(self.root, [])
        isolation = write_isolation_tsv(self.root)
        env = dict(os.environ)
        env["HOME"] = str(fake_home)
        return subprocess.run(
            [
                sys.executable, str(RUNNER),
                "--root", str(self.root),
                "--baseline", str(baseline),
                "--isolation-tsv", str(isolation),
                "--isolation=isolated",
                "--report", str(self.root / "leak-report.tsv"),
                *extra_args,
            ],
            cwd=str(ROOT),
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )

    def _fast_own_writer_home(self, marker: str) -> Path:
        fake_home = Path(tempfile.mkdtemp(prefix="w1-fake-home-fastown-"))
        live_dir = fake_home / ".local" / "state" / "hearting"
        live_dir.mkdir(parents=True)
        write_suite(
            self.root,
            "fastwriter.test.py",
            f"""
            from pathlib import Path
            p = Path({str(live_dir)!r}) / {marker!r}
            with open(p, "w") as f:
                f.write("{{}}")
            """,
        )
        return fake_home

    def _fast_foreign_writer_home(self, marker: str):
        """A concurrent session's fast write: opened, written and closed from
        outside any suite PGID. Gated on a ready file the suite itself
        creates, so the write always lands inside the runner's before/after
        live-state window without depending on runner start-up timing."""
        fake_home = Path(tempfile.mkdtemp(prefix="w1-fake-home-fastforeign-"))
        live_dir = fake_home / ".local" / "state" / "hearting"
        live_dir.mkdir(parents=True)
        ready = self.root / "suite-ready"
        write_suite(
            self.root,
            "harmless.test.py",
            f"""
            from pathlib import Path
            import time
            Path({str(ready)!r}).write_text("1")
            time.sleep(1.0)
            """,
        )

        import threading as _threading

        def _write_foreign():
            deadline = time.time() + 30
            while not ready.exists() and time.time() < deadline:
                time.sleep(0.01)
            with open(live_dir / marker, "w") as f:
                f.write("{}")

        writer = _threading.Thread(target=_write_foreign, daemon=True)
        return fake_home, writer, ready

    def test_fast_foreign_write_is_advisory_not_fatal(self):
        # The defect this contract replaces: a concurrent session's fast
        # write killed an otherwise-green local run.
        fake_home, writer, _ready = self._fast_foreign_writer_home("fast-foreign.json")
        writer.start()
        try:
            result = self._leak_sweep_run(fake_home)
        finally:
            writer.join(timeout=30)
        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, 0, output)
        self.assertIn("LIVE-STATE-UNATTRIBUTED: ", result.stderr)
        self.assertIn("live-state-unattributed=1", result.stderr)
        self.assertNotIn("LIVE-STATE-LEAK:", result.stderr)

    def test_fast_foreign_write_is_fatal_under_strict_leak_sweep(self):
        # Single-tenant mode (CI): with no concurrent writer possible, an
        # unattributable write is this run's own and must fail it.
        fake_home, writer, _ready = self._fast_foreign_writer_home("fast-foreign-strict.json")
        writer.start()
        try:
            result = self._leak_sweep_run(fake_home, "--strict-leak-sweep")
        finally:
            writer.join(timeout=30)
        output = result.stdout + result.stderr
        self.assertNotEqual(result.returncode, 0, output)
        self.assertIn("LIVE-STATE-UNATTRIBUTED: ", result.stderr)
        self.assertIn("__live_state_unattributed__", output)

    def test_fast_own_write_is_never_a_silent_pass(self):
        # An own fast write is indistinguishable from a foreign one at the
        # evidence level, so it is non-fatal by default -- but it must always
        # be visible, never silently swallowed.
        fake_home = self._fast_own_writer_home("fast-marker.json")
        result = self._leak_sweep_run(fake_home)
        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, 0, output)
        self.assertIn("LIVE-STATE-UNATTRIBUTED: ", result.stderr)
        self.assertIn("live-state-unattributed=1", result.stderr)

    def test_fast_own_write_is_fatal_under_strict_leak_sweep(self):
        fake_home = self._fast_own_writer_home("fast-marker-strict.json")
        result = self._leak_sweep_run(fake_home, "--strict-leak-sweep")
        output = result.stdout + result.stderr
        self.assertNotEqual(result.returncode, 0, output)
        self.assertIn("__live_state_unattributed__", output)


class LiveStateSamplerUnitFixture(unittest.TestCase):
    """Unit-level coverage of the bounded PGID sampler itself: stop/join
    behavior and lock-protected snapshotting, independent of a real suite
    subprocess or the live-state root."""

    def setUp(self):
        self.mod = load_runner_module()

    def test_sampler_stops_promptly_on_event_and_snapshot_is_consistent(self):
        import threading

        tmp = Path(tempfile.mkdtemp(prefix="w1-sampler-"))
        try:
            fake_live_root = tmp / "live"
            fake_live_root.mkdir()
            self.mod.LIVE_STATE_ROOT = fake_live_root
            stop_event = threading.Event()
            collected: set[str] = set()
            lock = threading.Lock()
            marker = fake_live_root / "marker.txt"
            with open(marker, "w") as f:
                f.write("x")
                # Sample this test process's own process group -- its held-open
                # fd under the (overridden) live-state root stands in for a
                # suite child the sampler would otherwise track by real PGID.
                thread = threading.Thread(
                    target=self.mod._sample_pgid_live_state_writes,
                    args=(os.getpgid(0), stop_event, collected, lock),
                    daemon=True,
                )
                thread.start()
                time.sleep(0.5)
                stop_event.set()
                started_join = time.time()
                thread.join(timeout=2.0)
                join_elapsed = time.time() - started_join
            self.assertFalse(thread.is_alive())
            self.assertLess(join_elapsed, 2.0)
            with lock:
                snapshot = set(collected)
            self.assertIn(str(marker), snapshot)
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)


class SeedingGateFixture(unittest.TestCase):
    """owner addendum C / required regression 10: --compare-baseline is the
    mandatory mechanical seeding gate — this duplicates the CompareBaselineFixture
    intent under the name the plan calls out explicitly."""

    def test_regression_blocks_seeding(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            header = "\t".join(
                ["suite_path", "test_id", "verdict", "kind", "isolation_profile", "duration_s", "detail"]
            )
            base = root / "base.tsv"
            base.write_text("\n".join(["# r", header, "x.test.py\t-\tFAIL\t\tisolated\t0.01\t"]) + "\n")
            head = root / "head.tsv"
            head.write_text(
                "\n".join(
                    ["# r", header, "x.test.py\t-\tFAIL\t\tisolated\t0.01\t", "y.test.py\t-\tFAIL\t\tisolated\t0.01\t"]
                )
                + "\n"
            )
            result = run_runner(["--compare-baseline", "--base-report", str(base), "--head-report", str(head)])
            self.assertNotEqual(result.returncode, 0)
            payload = json.loads(result.stdout)
            self.assertTrue(payload["regression"])


class ShardPartitionTest(unittest.TestCase):
    """--shard: a deterministic, duration-balanced split of the selected suites."""

    def setUp(self):
        self.mod = load_runner_module()
        self.paths = [f"s{n:02d}.test.py" for n in range(23)]
        self.durations = {p: float((n * 7) % 11 + 1) for n, p in enumerate(self.paths)}

    def test_every_suite_lands_in_exactly_one_shard(self):
        shards = self.mod.partition_shards(self.paths, self.durations, 4)
        flat = [p for shard in shards for p in shard]
        self.assertEqual(sorted(flat), sorted(self.paths))
        self.assertEqual(len(flat), len(set(flat)))

    def test_split_is_deterministic_and_input_order_independent(self):
        first = self.mod.partition_shards(self.paths, self.durations, 4)
        again = self.mod.partition_shards(list(reversed(self.paths)), dict(self.durations), 4)
        self.assertEqual(first, again)

    def test_shards_are_balanced_by_duration(self):
        shards = self.mod.partition_shards(self.paths, self.durations, 4)
        totals = [sum(self.durations[p] for p in shard) for shard in shards]
        self.assertLessEqual(max(totals) - min(totals), max(self.durations.values()))

    def test_equal_weights_tie_break_by_path_then_lowest_shard(self):
        shards = self.mod.partition_shards(["b.test.py", "a.test.py", "c.test.py"], {}, 2)
        self.assertEqual(shards, [["a.test.py", "c.test.py"], ["b.test.py"]])

    def test_unknown_suite_takes_the_median_of_known_durations(self):
        weights = self.mod.shard_weights(["a", "b", "new"], {"a": 2.0, "b": 8.0})
        self.assertEqual(weights["new"], 5.0)
        self.assertEqual(self.mod.shard_weights(["new"], {})["new"], 1.0)

    def test_more_shards_than_suites_leaves_empty_shards(self):
        shards = self.mod.partition_shards(["a.test.py"], {}, 3)
        self.assertEqual(shards, [["a.test.py"], [], []])

    def test_shard_spec_grammar(self):
        self.assertEqual(self.mod.parse_shard("2/4"), (2, 4))
        self.assertEqual(self.mod.parse_shard("1/1"), (1, 1))
        for bad in ("0/4", "5/4", "1/0", "a/b", "1", "1/2/3", "-1/4", ""):
            with self.assertRaises(ValueError, msg=bad):
                self.mod.parse_shard(bad)

    def write_table(self, root: Path, body: str) -> Path:
        path = root / "durations.tsv"
        path.write_text(body, encoding="utf-8")
        return path

    def test_duration_table_errors_are_explicit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            good = self.write_table(root, "# c\nsuite_path\tduration_s\na.test.py\t1.5\n")
            self.assertEqual(self.mod.load_durations(good), {"a.test.py": 1.5})
            cases = {
                "missing header": "a.test.py\t1.5\n",
                "bad number": "suite_path\tduration_s\na.test.py\tfast\n",
                "negative": "suite_path\tduration_s\na.test.py\t-1\n",
                "nan": "suite_path\tduration_s\na.test.py\tnan\n",
                "short row": "suite_path\tduration_s\na.test.py\n",
                "duplicate": "suite_path\tduration_s\na.test.py\t1\na.test.py\t2\n",
                "empty": "# only a comment\n",
            }
            for name, body in cases.items():
                with self.assertRaises(self.mod.BaselineError, msg=name):
                    self.mod.load_durations(self.write_table(root, body))
            with self.assertRaises(self.mod.BaselineError):
                self.mod.load_durations(root / "absent.tsv")

    def test_report_duration_rows_collapse_only_identical_replicas(self):
        rows = [
            {"suite_path": "a.test.py", "duration_s": "1.25", "verdict": "KNOWN-FAIL"},
            {"suite_path": "a.test.py", "duration_s": "1.25", "verdict": "KNOWN-FAIL"},
            {"suite_path": "b.test.py", "duration_s": "4.0", "verdict": "FLAKY-PASS"},
        ]
        self.assertEqual(self.mod.collapse_replicated_report_durations(rows),
                         {"a.test.py": 1.25, "b.test.py": 4.0})

    def test_report_duration_conflict_is_ambiguous_not_selected_by_order(self):
        rows = [{"suite_path": "a.test.py", "duration_s": "1.25"},
                {"suite_path": "a.test.py", "duration_s": "2.0"}]
        with self.assertRaisesRegex(self.mod.BaselineError, "ambiguous duplicate duration"):
            self.mod.collapse_replicated_report_durations(rows)

    def test_committed_table_is_well_formed(self):
        # The table only balances shards: a suite without a row is weighed at the
        # median (shard_weights) and a row for a removed suite is ignored, so a PR
        # that adds or removes a test file must not have to edit this table.
        durations = self.mod.load_durations(ROOT / "tools" / "test-durations.tsv")
        self.assertTrue(durations)
        self.assertTrue(all(value >= 0 for value in durations.values()))
        corpus = sorted(self.mod.suite_relpath(ROOT, s) for s in self.mod.collect_suites(ROOT))
        weights = self.mod.shard_weights(corpus, durations)
        self.assertEqual(set(weights), set(corpus))


class ShardCliFixture(RunTestsFixtureBase):
    """--shard / --suite-list / --verify-shards through the real CLI."""

    def setUp(self):
        super().setUp()
        for name in ("a", "b", "c", "d", "e"):
            write_suite(self.root, f"{name}.test.py", "import sys\nsys.exit(0)\n")
        write_suite(self.root, "sub/f.test.py", "import sys\nsys.exit(0)\n")
        self.table = self.root / "durations.tsv"
        self.table.write_text(
            "suite_path\tduration_s\na.test.py\t9\nb.test.py\t5\nc.test.py\t4\n"
            "d.test.py\t3\ne.test.py\t2\nsub/f.test.py\t1\n",
            encoding="utf-8",
        )

    def base_args(self, baseline_rows=None, isolation_rows=None):
        return [
            "--root", str(self.root),
            "--baseline", str(write_baseline(self.root, baseline_rows or [])),
            "--isolation-tsv", str(write_isolation_tsv(self.root, isolation_rows)),
            "--durations", str(self.table),
            "--isolation=isolated", "--jobs", "2", "--timeout", "5", "--no-leak-sweep",
        ]

    def run_shard(self, index: int, count: int, extra=None, listing=True, attempt=None):
        suffix = f"-attempt-{attempt}" if attempt is not None else ""
        out = self.root / "out" / f"run-tests-report-shard-{index}-of-{count}{suffix}"
        out.mkdir(parents=True, exist_ok=True)
        args = self.base_args() + ["--shard", f"{index}/{count}", "--report", str(out / "run-tests-report.tsv")]
        if listing:
            args += ["--suite-list", str(out / "suite-list.tsv")]
        result = run_runner(args + (extra or []))
        return result, out

    def write_census(self, extra=None):
        census = self.root / "out" / "census-suite-list.tsv"
        census.parent.mkdir(parents=True, exist_ok=True)
        result = run_runner(self.base_args() + ["--census", "--suite-list", str(census)] + (extra or []))
        self.assertEqual(result.returncode, 0, result.stderr)
        return census

    def manifest(self, path: Path) -> list[str]:
        return path.read_text(encoding="utf-8").splitlines()

    def run_all_shards(self, count=3, attempt=None):
        self.write_census()
        for index in range(1, count + 1):
            result, _ = self.run_shard(index, count, attempt=attempt)
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)

    def copy_attempt(self, source: Path, attempt: int) -> Path:
        import shutil
        target = source.with_name(source.name.split("-attempt-")[0] + f"-attempt-{attempt}")
        return Path(shutil.copytree(source, target))

    def verify(self):
        directory = self.root / "out"

        def snapshot():
            return {p.relative_to(directory): p.read_bytes() for p in directory.rglob("*") if p.is_file()}

        before = snapshot()
        result = run_runner(["--verify-shards", str(directory)])
        self.assertEqual(snapshot(), before, "verification changed artifact files or bytes")
        return result

    def assert_selected(self, result, expected):
        lines = []
        for index, count, attempt in expected:
            suffix = f"-attempt-{attempt}" if attempt is not None else ""
            label = attempt if attempt is not None else "legacy"
            lines.append(f"selected shard={index}/{count} attempt={label} "
                         f"directory=run-tests-report-shard-{index}-of-{count}{suffix}")
        self.assertEqual([line for line in result.stdout.splitlines() if line.startswith("selected ")], lines)

    def test_shard_census_sums_to_the_unsharded_census(self):
        total = 0
        for index in range(1, 4):
            result = run_runner(self.base_args() + ["--census", "--shard", f"{index}/3"])
            self.assertEqual(result.returncode, 0, result.stderr)
            total += int(re.search(r"collected=(\d+)", result.stdout).group(1))
        whole = run_runner(self.base_args() + ["--census"])
        self.assertEqual(total, int(re.search(r"collected=(\d+)", whole.stdout).group(1)))
        self.assertEqual(total, 6)

    def test_manifest_is_written_before_the_run_and_has_the_agreed_shape(self):
        result, out = self.run_shard(1, 3)
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = self.manifest(out / "suite-list.tsv")
        self.assertEqual(lines[:2], ["# shard=1/3", "suite_path"])
        self.assertEqual(lines[2:], sorted(lines[2:]))
        census = self.manifest(self.write_census())
        self.assertEqual(census[:2], ["# shard=all", "suite_path"])
        self.assertEqual(len(census) - 2, 6)

    def test_a_shard_reports_only_its_own_suites(self):
        result, out = self.run_shard(2, 3)
        self.assertEqual(result.returncode, 0, result.stderr)
        mine = set(self.manifest(out / "suite-list.tsv")[2:])
        reported = {
            line.split("\t")[0]
            for line in (out / "run-tests-report.tsv").read_text(encoding="utf-8").splitlines()[2:]
        }
        self.assertEqual(reported, mine)
        self.assertTrue(mine)

    def test_all_shards_together_verify_against_the_census(self):
        self.run_all_shards()
        result = self.verify()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("missing=0 duplicate=0", result.stdout)
        self.assertIn("shards=3/3", result.stdout)

    def test_first_attempt_verifies_each_shard_once(self):
        self.run_all_shards(2, attempt=1)
        result = self.verify()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("census=6 assigned=6 shards=2/2 missing=0 duplicate=0", result.stdout)
        self.assert_selected(result, [(1, 2, 1), (2, 2, 1)])

    def test_partial_rerun_preserves_payloads_and_never_falls_back_for_a_missing_verdict(self):
        self.run_all_shards(2, attempt=9)
        for out in (self.root / "out").glob("run-tests-report-shard-*"):
            # Representative upload payloads that coverage must not touch.
            (out / "run-tests-seed-baseline.tsv").write_bytes(b"seed\tfixture\n")
            (out / "progress.log").write_bytes(b"attempt 9\n\x00")
            diagnostics = out / "run-tests-diagnostics" / "suite" / "stderr.log"
            diagnostics.parent.mkdir(parents=True)
            diagnostics.write_bytes(b"original diagnostic\n\xff")
        old = self.root / "out" / "run-tests-report-shard-2-of-2-attempt-9"
        latest = self.copy_attempt(old, 10)
        result = self.verify()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("shards=2/2 missing=0 duplicate=0", result.stdout)
        self.assert_selected(result, [(1, 2, 9), (2, 2, 10)])
        report = latest / "run-tests-report.tsv"
        lines = self.manifest(report)
        victim = lines[2].split("\t")[0]
        self.assertIn(victim, (old / report.name).read_text())
        report.write_text("\n".join(lines[:2] + lines[3:]) + "\n")
        result = self.verify()
        self.assertEqual(result.returncode, 1)
        self.assert_selected(result, [(1, 2, 9), (2, 2, 10)])
        self.assertIn(f"report-missing: {victim} (shard 2)", result.stdout)

    def test_full_rerun_selects_the_numeric_latest_in_either_creation_order(self):
        self.run_all_shards(2, attempt=10)
        for index in (1, 2):
            newer = self.root / "out" / f"run-tests-report-shard-{index}-of-2-attempt-10"
            self.copy_attempt(newer, 9)  # Older attempt created after the newer one.
        result = self.verify()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assert_selected(result, [(1, 2, 10), (2, 2, 10)])
        # Now create the newest attempt after both older ones, for every shard.
        for index in (1, 2):
            self.copy_attempt(self.root / "out" / f"run-tests-report-shard-{index}-of-2-attempt-9", 11)
        result = self.verify()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assert_selected(result, [(1, 2, 11), (2, 2, 11)])

    def test_latest_verdict_is_selected_without_filtering_for_pass(self):
        self.write_census()
        for old_exit, latest_exit in ((1, 0), (0, 1)):
            with self.subTest(old_exit=old_exit, latest_exit=latest_exit):
                outputs = []
                for attempt, code in ((9, old_exit), (10, latest_exit)):
                    write_suite(self.root, "a.test.py", f"import sys\nsys.exit({code})\n")
                    result, out = self.run_shard(1, 1, attempt=attempt)
                    self.assertEqual(result.returncode, code, result.stdout + result.stderr)
                    verdict = b"FAIL" if code else b"PASS"
                    self.assertIn(b"a.test.py\t-\t" + verdict + b"\t", (out / "run-tests-report.tsv").read_bytes())
                    outputs.append(out)
                result = self.verify()
                # A complete FAIL report covers its suites; the job status is
                # checked separately by the workflow's SHARDS guard.
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assert_selected(result, [(1, 1, 10)])
                if old_exit:
                    (outputs[0] / "run-tests-report.tsv").unlink()
                    (outputs[0] / "suite-list.tsv").write_text("interrupted old attempt\n")
                    self.assertEqual(self.verify().returncode, 0)

    def test_latest_broken_payloads_are_not_hidden_by_an_older_pass(self):
        self.run_all_shards(2, attempt=9)
        latest = self.copy_attempt(self.root / "out" / "run-tests-report-shard-2-of-2-attempt-9", 10)
        originals = {p: p.read_bytes() for p in latest.iterdir()}
        cases = [
            ("run-tests-report.tsv", None, "run-tests-report.tsv unreadable"),
            ("run-tests-report.tsv", b"wrong header\n", "expected header"),
            ("suite-list.tsv", None, "suite-list.tsv"),
            ("suite-list.tsv", b"wrong header\n", "not a suite manifest"),
            ("suite-list.tsv", b"# shard=1/2\nsuite_path\n", "manifest says '# shard=1/2'"),
            ("suite-list.tsv", b"# shard=all\nsuite_path\n", "manifest says '# shard=all'"),
        ]
        for filename, contents, error in cases:
            with self.subTest(filename=filename, contents=contents):
                for path, data in originals.items():
                    path.write_bytes(data)
                target = latest / filename
                if contents is None:
                    target.unlink()
                else:
                    target.write_bytes(contents)
                result = self.verify()
                self.assertEqual(result.returncode, 1)
                self.assert_selected(result, [(1, 2, 9), (2, 2, 10)])
                self.assertIn(error, result.stderr)

    def test_same_numeric_attempt_alias_is_an_error_even_when_superseded(self):
        import shutil
        self.run_all_shards(2, attempt=9)
        original = self.root / "out" / "run-tests-report-shard-2-of-2-attempt-9"
        alias = original.with_name("run-tests-report-shard-02-of-2-attempt-09")
        shutil.copytree(original, alias)
        for superseded in (False, True):
            with self.subTest(superseded=superseded):
                if superseded:
                    self.copy_attempt(original, 10)
                result = self.verify()
                self.assertEqual(result.returncode, 1)
                self.assertIn("shard 2/2 attempt 9 appears twice", result.stderr)
                self.assertIn(original.name, result.stderr)
                self.assertIn(alias.name, result.stderr)

    def test_legacy_shards_can_mix_with_explicit_attempts(self):
        self.run_all_shards(2)
        legacy = self.root / "out" / "run-tests-report-shard-2-of-2"
        self.copy_attempt(legacy, 1)
        (legacy / "run-tests-report.tsv").unlink()
        result = self.verify()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assert_selected(result, [(1, 2, None), (2, 2, 1)])

    def test_attempt_selection_preserves_count_and_index_errors(self):
        import shutil
        self.run_all_shards(2, attempt=9)
        original = self.root / "out" / "run-tests-report-shard-1-of-2-attempt-9"
        for index, count, error in ((1, 3, "disagree on the shard count"), (3, 2, "not exactly 1..2")):
            with self.subTest(index=index, count=count):
                alias = original.with_name(f"run-tests-report-shard-{index}-of-{count}-attempt-10")
                shutil.copytree(original, alias)
                manifest = alias / "suite-list.tsv"
                manifest.write_text(manifest.read_text().replace("# shard=1/2", f"# shard={index}/{count}"))
                result = self.verify()
                self.assertEqual(result.returncode, 1)
                self.assertIn(error, result.stderr)
                shutil.rmtree(alias)
        shutil.rmtree(self.root / "out" / "run-tests-report-shard-2-of-2-attempt-9")
        result = self.verify()
        self.assertEqual(result.returncode, 1)
        self.assertIn("not exactly 1..2", result.stderr)

    def test_selected_attempt_keeps_census_and_non_verdict_validation(self):
        self.run_all_shards(2, attempt=9)
        latest = self.copy_attempt(self.root / "out" / "run-tests-report-shard-2-of-2-attempt-9", 10)
        manifest = latest / "suite-list.tsv"
        report = latest / "run-tests-report.tsv"
        census = self.root / "out" / "census-suite-list.tsv"
        originals = {p: p.read_text() for p in (manifest, report, census)}
        victim = self.manifest(manifest)[2]
        lines = originals[manifest].splitlines()
        report_row = next(row for row in originals[report].splitlines()[2:] if row.split("\t")[0] == victim)
        cases = [
            (manifest, "\n".join(lines[:2] + lines[3:]) + "\n", f"missing: {victim}"),
            (manifest, originals[manifest] + victim + "\n", f"duplicate: {victim}"),
            (manifest, originals[manifest] + "extra.test.py\n", "extra: extra.test.py"),
            (census, originals[census] + victim + "\n", "repeats a suite path"),
            (report, originals[report] + report_row.replace(victim, "extra.test.py", 1) + "\n", "report-extra: extra.test.py"),
        ]
        for verdict in ("STALE", "UNDECLARED-ISOLATION-OPTOUT", "ISOLATION-OPTOUT-UNNEEDED"):
            cases.append((report, originals[report].replace(report_row, report_row.replace("\tPASS\t", f"\t{verdict}\t")),
                          f"report-missing: {victim}"))
        for path, contents, error in cases:
            with self.subTest(path=path.name, error=error):
                for target, text in originals.items():
                    target.write_text(text)
                path.write_text(contents)
                result = self.verify()
                self.assertEqual(result.returncode, 1)
                self.assertIn(error, result.stdout + result.stderr)

    def test_select_and_exclude_apply_before_the_split(self):
        census = self.write_census(["--exclude", "sub/*", "--exclude", "e.test.py"])
        self.assertEqual(len(self.manifest(census)) - 2, 4)
        result, out = self.run_shard(1, 2, ["--select", "*.test.py", "--exclude", "a.test.py"])
        self.assertEqual(result.returncode, 0, result.stderr)
        listed = set(self.manifest(out / "suite-list.tsv")[2:])
        self.assertNotIn("a.test.py", listed)
        self.assertLessEqual(listed, {"b.test.py", "c.test.py", "d.test.py", "e.test.py", "sub/f.test.py"})

    def test_invalid_shard_specs_and_duplicates_are_usage_errors_before_any_run(self):
        for spec in ("0/4", "5/4", "a/b", "2/0"):
            result = run_runner(self.base_args() + ["--shard", spec, "--report", str(self.root / "r.tsv")])
            self.assertEqual(result.returncode, 64, spec + result.stderr)
            self.assertFalse((self.root / "r.tsv").exists(), spec)
        result = run_runner(self.base_args() + ["--shard", "1/2", "--shard", "2/2"])
        self.assertEqual(result.returncode, 64, result.stderr)

    def test_missing_or_broken_duration_table_is_an_error_not_a_guess(self):
        self.table.write_text("suite_path\tduration_s\na.test.py\tsoon\n", encoding="utf-8")
        result, _ = self.run_shard(1, 2)
        self.assertEqual(result.returncode, 65)
        self.assertIn("duration table error", result.stderr)
        self.table.unlink()
        result, _ = self.run_shard(1, 2)
        self.assertEqual(result.returncode, 65)

    def test_a_suite_missing_from_the_table_still_gets_exactly_one_shard(self):
        write_suite(self.root, "brand_new.test.py", "import sys\nsys.exit(0)\n")
        self.run_all_shards()
        result = self.verify()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("census=7", result.stdout)

    def test_verifier_rejects_a_dropped_suite(self):
        self.run_all_shards()
        target = self.root / "out" / "run-tests-report-shard-1-of-3" / "suite-list.tsv"
        lines = self.manifest(target)
        dropped = lines.pop()
        target.write_text("\n".join(lines) + "\n", encoding="utf-8")
        result = self.verify()
        self.assertEqual(result.returncode, 1)
        self.assertIn(f"missing: {dropped}", result.stdout)

    def test_verifier_rejects_a_duplicated_suite(self):
        self.run_all_shards()
        first = self.manifest(self.root / "out" / "run-tests-report-shard-1-of-3" / "suite-list.tsv")[2]
        other = self.root / "out" / "run-tests-report-shard-2-of-3" / "suite-list.tsv"
        other.write_text(other.read_text(encoding="utf-8") + first + "\n", encoding="utf-8")
        result = self.verify()
        self.assertEqual(result.returncode, 1)
        self.assertIn(f"duplicate: {first}", result.stdout)

    def test_verifier_rejects_a_missing_shard_number(self):
        self.run_all_shards()
        import shutil
        shutil.rmtree(self.root / "out" / "run-tests-report-shard-3-of-3")
        result = self.verify()
        self.assertEqual(result.returncode, 1)
        self.assertIn("not exactly 1..3", result.stderr)

    def test_verifier_rejects_disagreeing_shard_counts(self):
        self.run_all_shards()
        (self.root / "out" / "run-tests-report-shard-3-of-3").rename(self.root / "out" / "run-tests-report-shard-3-of-4")
        result = self.verify()
        self.assertEqual(result.returncode, 1)

    def test_verifier_rejects_a_manifest_without_a_report(self):
        self.run_all_shards()
        (self.root / "out" / "run-tests-report-shard-2-of-3" / "run-tests-report.tsv").unlink()
        result = self.verify()
        self.assertEqual(result.returncode, 1)
        self.assertIn("run-tests-report.tsv unreadable", result.stderr)

    def test_verifier_rejects_a_suite_with_no_verdict_in_its_report(self):
        self.run_all_shards()
        report = self.root / "out" / "run-tests-report-shard-1-of-3" / "run-tests-report.tsv"
        lines = report.read_text(encoding="utf-8").splitlines()
        victim = lines[2].split("\t")[0]
        report.write_text("\n".join(lines[:2] + lines[3:]) + "\n", encoding="utf-8")
        result = self.verify()
        self.assertEqual(result.returncode, 1)
        self.assertIn(f"report-missing: {victim} (shard 1)", result.stdout)

    def test_verifier_needs_a_census_and_at_least_one_shard(self):
        empty = self.root / "empty"
        empty.mkdir()
        self.assertEqual(run_runner(["--verify-shards", str(empty)]).returncode, 1)
        self.write_census()
        result = self.verify()
        self.assertEqual(result.returncode, 1)
        self.assertIn("no run-tests-report-shard", result.stderr)

    def test_stale_baseline_row_is_reported_by_shard_one_only(self):
        args = self.base_args(
            [f"gone.test.py\t-\texit-nonzero\tisolated\tgone\tMA-TEST-900\t{TOMORROW}"]
        )
        results = {}
        for index in (1, 2, 3):
            out = self.root / f"s{index}.tsv"
            results[index] = (run_runner(args + ["--shard", f"{index}/3", "--report", str(out)]), out)
        self.assertNotEqual(results[1][0].returncode, 0)
        self.assertIn("STALE", results[1][1].read_text(encoding="utf-8"))
        for index in (2, 3):
            self.assertEqual(results[index][0].returncode, 0, results[index][0].stderr)
            self.assertNotIn("STALE", results[index][1].read_text(encoding="utf-8"))

    def test_another_shards_baseline_and_isolation_rows_are_not_stale_here(self):
        # Rows for real suites that live in another shard must be invisible to
        # this shard: not STALE, not ISOLATION-OPTOUT-UNNEEDED, not failing.
        owner = {}
        for index in (1, 2):
            self.run_shard(index, 2)
            for rel in self.manifest(self.root / "out" / f"run-tests-report-shard-{index}-of-2" / "suite-list.tsv")[2:]:
                owner[rel] = index
        other = next(rel for rel, index in owner.items() if index == 2)
        args = self.base_args(
            [f"{other}\t-\texit-nonzero\tisolated\tsibling\tMA-TEST-901\t{TOMORROW}"],
            [f"{other}\tinstalled-layout\tsibling\tMA-TEST-902\t{TOMORROW}"],
        )
        out = self.root / "x.tsv"
        result = run_runner(args + ["--shard", "1/2", "--report", str(out)])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        text = out.read_text(encoding="utf-8")
        self.assertNotIn("STALE", text)
        self.assertNotIn("ISOLATION-OPTOUT-UNNEEDED", text)

    def test_known_fail_xpass_and_seed_keep_their_meaning_inside_a_shard(self):
        write_suite(self.root, "kf.test.py", "import sys\nsys.exit(1)\n")
        write_suite(self.root, "xp.test.py", "import sys\nsys.exit(0)\n")
        self.table.write_text(self.table.read_text(encoding="utf-8") + "kf.test.py\t1\nxp.test.py\t1\n", encoding="utf-8")
        args = self.base_args([
            f"kf.test.py\t-\texit-nonzero\tisolated\tknown\tMA-TEST-903\t{TOMORROW}",
            f"xp.test.py\t-\texit-nonzero\tisolated\tknown\tMA-TEST-904\t{TOMORROW}",
        ])
        seen = {}
        for index in (1, 2):
            out = self.root / f"kx{index}.tsv"
            seed = self.root / f"seed{index}.tsv"
            result = run_runner(args + ["--shard", f"{index}/2", "--xpass-nonfatal", "--seed-baseline", str(seed),
                                        "--report", str(out)])
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertTrue(seed.exists())
            for line in out.read_text(encoding="utf-8").splitlines()[2:]:
                cols = line.split("\t")
                seen[cols[0]] = cols[2]
        self.assertEqual(seen["kf.test.py"], "KNOWN-FAIL")
        self.assertEqual(seen["xp.test.py"], "XPASS")

    def test_a_failing_suite_fails_only_its_own_shard(self):
        write_suite(self.root, "bad.test.py", "import sys\nsys.exit(1)\n")
        self.table.write_text(self.table.read_text(encoding="utf-8") + "bad.test.py\t1\n", encoding="utf-8")
        codes = [self.run_shard(index, 2)[0].returncode for index in (1, 2)]
        self.assertEqual(sorted(codes), [0, 1])

    def test_leak_sweep_stays_on_inside_a_shard(self):
        args = [a for a in self.base_args() if a != "--no-leak-sweep"]
        isolated_home = self.root / "runner-home"
        isolated_home.mkdir()
        env = dict(CLEAN_ENV)
        env["HOME"] = str(isolated_home)
        result = subprocess.run(
            [sys.executable, str(RUNNER)] + args + [
                "--shard", "1/2", "--strict-leak-sweep",
            ],
            cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=120,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_help_says_shard_works_with_every_isolation_profile(self):
        result = run_runner(["--help"])
        self.assertIn("every --isolation profile", " ".join(result.stdout.split()))


class ShardExecutionPolicyFixture(unittest.TestCase):
    """A sharded run of the real repository keeps the full-run limits."""

    def setUp(self):
        self.mod = load_runner_module()

    def args(self, *extra):
        return self.mod.build_arg_parser().parse_args(list(extra))

    def test_whole_repository_shard_requires_retry_and_caps_jobs(self):
        errors = self.mod.execution_policy_errors(
            self.args("--shard", "1/4", "--jobs", "5"), self.mod.ROOT.resolve()
        )
        self.assertIn("full repository runs require --retries 1 or greater", errors)
        self.assertTrue(any("--jobs <= 4" in error for error in errors), errors)

    def test_selected_shard_keeps_narrow_diagnostic_freedom(self):
        errors = self.mod.execution_policy_errors(
            self.args("--shard", "1/4", "--select", "tools/*.test.py", "--jobs", "8"),
            self.mod.ROOT.resolve(),
        )
        self.assertEqual(errors, [])


class NoAmbientExecutionPathFixture(unittest.TestCase):
    """Required regression 9 (owner addendum A): no subcommand or flag hands a
    suite subprocess the caller's ambient environment."""

    def test_isolation_flag_has_no_ambient_choice(self):
        # "ambient" may appear descriptively (documenting the absence of an
        # ambient path); the binding assertion is that no --isolation value
        # or subcommand selects one. Extract the {choices} set from --help.
        result = run_runner(["--help"])
        self.assertEqual(result.returncode, 0)
        m = re.search(r"--isolation \{([^}]+)\}", result.stdout)
        self.assertIsNotNone(m, result.stdout)
        choices = m.group(1).split(",")
        self.assertNotIn("ambient", choices)
        self.assertEqual(set(choices), {"isolated", "installed-layout", "live-registry", "ci-like"})

    def test_source_never_launches_a_suite_subprocess_with_inherited_environ(self):
        source = RUNNER.read_text(encoding="utf-8")
        # subprocess.run(...) without an explicit env= kwarg inherits the
        # caller's ambient environment (os.environ) by default — the one
        # execution path this runner must never contain for a suite launch.
        import ast

        tree = ast.parse(source)
        offending = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                is_subprocess_run = (
                    isinstance(func, ast.Attribute)
                    and func.attr == "run"
                    and isinstance(func.value, ast.Name)
                    and func.value.id == "subprocess"
                )
                if not is_subprocess_run:
                    continue
                has_env_kw = any(kw.arg == "env" for kw in node.keywords)
                if not has_env_kw:
                    offending.append(node.lineno)
        self.assertEqual(offending, [], f"subprocess.run calls missing explicit env= at lines: {offending}")


class RoutedTestOutputDefaults(unittest.TestCase):
    def test_routed_cli_uses_test_node_report_and_preserves_explicit_paths(self):
        import importlib.util
        fixture_spec = importlib.util.spec_from_file_location(
            "artifact_producer_fixture_for_run_tests", ROOT / "utilities" / "artifact_producer.test.py")
        fixture_module = importlib.util.module_from_spec(fixture_spec)
        fixture_spec.loader.exec_module(fixture_module)
        fixture = fixture_module.ProducerTestBase("runTest")
        fixture.setUp()
        runtime_home = fixture._env.get("AGENT_HOME") or str(ROOT)
        try:
            os.environ["AGENT_HOME"] = runtime_home
            fixture.activate()
            route, route_file = fixture.route("standard", "autopilot-code", "dev")
            begun = fixture_module.P.begin(fixture.root, route_file=route_file,
                capability="autopilot-code", intensity="standard", require_cycle=True)
            node = next(row for row in route["nodes"] if row.get("id") in {"test", "verification"})
            env = {**CLEAN_ENV, "AGENT_HOME": runtime_home,
                "AGENT_DISPATCH_JOBS": str(fixture.jobs),
                "AGENT_DISPATCH_WORKTREE": str(ROOT),
                "AGENT_ROUTE_FILE": str(route_file), "AGENT_ROUTE_ID": route["route_id"],
                "AGENT_ROUTE_NODE": node["id"], "AGENT_ARTIFACT_ROOT": str(fixture.root),
                **begun["env"]}
            common = [sys.executable, str(RUNNER), "--select", "utilities/artifact_context.test.py",
                      "--jobs", "1", "--timeout", "30"]
            routed = subprocess.run(common, cwd=ROOT, env=env, text=True, capture_output=True, timeout=90)
            self.assertEqual(routed.returncode, 0, routed.stdout + routed.stderr)
            default_report = Path(begun["cycle_dir"]) / "artifacts/test_logs/run-tests-report.tsv"
            self.assertTrue(default_report.is_file())

            explicit_report = Path(begun["cycle_dir"]) / "artifacts/test_logs/explicit-report.tsv"
            explicit_diagnostics = Path(begun["cycle_dir"]) / "artifacts/test_logs/explicit-diagnostics"
            explicit = subprocess.run([*common, "--root", str(ROOT), "--report", str(explicit_report),
                "--diagnostics-dir", str(explicit_diagnostics)], cwd=ROOT, env=env,
                text=True, capture_output=True, timeout=90)
            self.assertEqual(explicit.returncode, 0, explicit.stdout + explicit.stderr)
            self.assertTrue(explicit_report.is_file())

            standalone = run_runner(["--census"], cwd=ROOT)
            self.assertEqual(standalone.returncode, 0, standalone.stdout + standalone.stderr)
        finally:
            fixture.doCleanups()


if __name__ == "__main__":
    unittest.main()
