#!/usr/bin/env python3
"""Self-regression for tools/run-tests.py (plan Step 2.4 / owner addendum A & C).

Runs the real script as a subprocess against synthetic fixture roots so the
tests exercise the actual CLI contract, not internals.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
import textwrap
import unittest
from unittest import mock
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "tools" / "run-tests.py"


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

    def test_all_pass_is_xpass_hard_failure(self):
        self.write_retry_suite("all-pass")
        result, rows = self.run_fixture(self.baseline(), extra_args=["--retries", "1"])
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(rows[0]["verdict"], "XPASS")

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

    def baseline(self):
        return [
            "slow_flaky.test.py\t-\texit-nonzero\tisolated\t"
            f"flaky-timing: intermittent\tMA-RETRY-BUDGET\t{TOMORROW}"
        ]

    def test_exhausted_budget_is_recorded_and_skips_remaining_retries(self):
        self.write_slow_flaky_suite(1.0)
        result, rows = self.run_fixture(
            self.baseline(), extra_args=["--retries", "3", "--retry-budget", "1"]
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("retry-budget-exhausted", rows[0]["detail"])
        # budget stops the pass early: fewer attempts than requested ran
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
        write_suite(
            self.root,
            "leaky.test.py",
            f"""
            from pathlib import Path
            p = Path({str(fake_home / '.local' / 'state' / 'hearting')!r}) / "leak-marker.json"
            p.write_text("{{}}")
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


if __name__ == "__main__":
    unittest.main()
