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
import textwrap
import unittest
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "tools" / "run-tests.py"

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
    header = "suite_path\ttest_id\texpected_failure_kind\tisolation_profile\treason\tdefect_id\treview_by"
    p.write_text("\n".join(["# baseline", header] + rows) + "\n", encoding="utf-8")
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
        self.assertEqual(set(choices), {"isolated", "installed-layout", "live-registry"})

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
