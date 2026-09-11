from contextlib import contextmanager, redirect_stdout
import ast
from datetime import datetime, timedelta, timezone
import fcntl
import importlib.util
from io import StringIO
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock
from pathlib import Path
import sys
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "utilities"))
import dispatch_contract as D
import artifact_lifecycle as L
import artifact_producer as P
import dispatch_lifecycle as DL

_ROUTE_SPEC = importlib.util.spec_from_file_location(
    "review_binding_route_fixture", PROJECT_ROOT / "utilities/capability-route.py"
)
R = importlib.util.module_from_spec(_ROUTE_SPEC)
_ROUTE_SPEC.loader.exec_module(R)


class ReviewOutputBindingTest(unittest.TestCase):
    def _row(self, *, repo, worktree, root, output, attempt="att-review",
             extra="", status="open"):
        binding = {
            "schema_version": 2, "attempt_id": attempt, "cycle_id": "cyc-1",
            "producer_id": "prod-1", "worktree": str(worktree),
            "artifact_root": str(root), "capability": "autopilot-code",
            "unit": "qa/code-review", "output_path": str(output),
        }
        digest = D.review_output_binding_digest(binding)
        locator = D.encode_review_output_locator(output.relative_to(root).as_posix())
        metadata = (
            "attempt_schema_version=2,attempt_id=" + attempt + ",dispatch_depth=1,"
            "transport=headless,execution_surface=registered-headless,registered_worker=1,"
            "worker_type=review,unit=qa/code-review,capability=autopilot-code,"
            f"artifact_root={root},review_cycle_id=cyc-1,review_producer_id=prod-1,"
            f"review_output_locator_b64={locator},review_output_digest={digest}{extra}"
        )
        return f"ts\t{status}\t{repo}\t{worktree}\treview\t{metadata}\n"

    def _validate(self, jobs, output, worktree, root):
        return D.validate_review_output_binding(
            jobs, attempt_id="att-review", output_path=output,
            cycle_id="cyc-1", producer_id="prod-1", capability="autopilot-code",
            unit="qa/code-review", worktree=worktree, artifact_root=root,
        )

    def test_actual_six_column_row_uses_worktree_column_and_metadata_root(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td).resolve()
            worktree = base / "linked-worktree"
            root = base / "primary" / ".agent_reports"
            for path in (worktree, root):
                path.mkdir(parents=True)
            output = root / "campaigns/camp/cycle/artifacts/plans/report.md"
            jobs = base / "jobs.log"
            jobs.write_text(self._row(
                repo=worktree, worktree=worktree, root=root, output=output,
            ), encoding="utf-8")
            binding = self._validate(jobs, output, worktree, root)
            self.assertEqual(binding["artifact_root"], str(root))
            self.assertEqual(binding["worktree"], str(worktree))

    def test_malformed_matching_duplicate_fails_closed_but_unrelated_legacy_does_not(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td).resolve()
            worktree = base / "worktree"
            root = base / "artifact-root"
            worktree.mkdir()
            root.mkdir()
            output = root / "plans/report.md"
            jobs = base / "jobs.log"
            valid = self._row(
                repo=worktree, worktree=worktree, root=root, output=output,
            )
            unrelated = "ts\topen\t/repo\t/wt\tlegacy\tbroken-token,attempt_id=att-other\n"
            jobs.write_text(unrelated + valid, encoding="utf-8")
            self.assertEqual(self._validate(jobs, output, worktree, root)["attempt_id"], "att-review")

            malformed_duplicate = (
                f"ts\topen\t{worktree}\t{worktree}\treview\t"
                "broken-token,attempt_id=att-review,worker_type=review\n"
            )
            jobs.write_text(valid + malformed_duplicate, encoding="utf-8")
            with self.assertRaises(D.DispatchContractError) as caught:
                self._validate(jobs, output, worktree, root)
            self.assertEqual(caught.exception.reason, "review-attempt-row-malformed")
            malformed_columns = (
                f"ts\topen\t{worktree}\t{worktree}\treview\textra\t"
                "attempt_id=att-review,worker_type=review\n"
            )
            jobs.write_text(valid + malformed_columns, encoding="utf-8")
            with self.assertRaises(D.DispatchContractError) as caught:
                self._validate(jobs, output, worktree, root)
            self.assertEqual(caught.exception.reason, "review-attempt-row-malformed")

    def test_route_or_owner_route_bound_review_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td).resolve()
            worktree = base / "worktree"
            root = base / "artifact-root"
            worktree.mkdir()
            root.mkdir()
            output = root / "plans/report.md"
            jobs = base / "jobs.log"
            for extra in (
                ",route_id=rt-foreign,route_node=impl-review",
                ",owner_route_id=rt-owner,owner_route_file=/route.json",
            ):
                with self.subTest(extra=extra):
                    jobs.write_text(self._row(
                        repo=worktree, worktree=worktree, root=root,
                        output=output, extra=extra,
                    ), encoding="utf-8")
                    with self.assertRaises(D.DispatchContractError) as caught:
                        self._validate(jobs, output, worktree, root)
                    self.assertEqual(caught.exception.reason, "review-attempt-route-bound")

    def test_valid_exact_registered_review_binding(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            jobs = root / "jobs.log"
            output = root / "campaigns/camp/cycle/artifacts/plans/report.md"
            digest = D.review_output_binding_digest({
                "schema_version": 2, "attempt_id": "att-review", "cycle_id": "cyc-1",
                "producer_id": "prod-1", "worktree": str(root), "artifact_root": str(root),
                "capability": "autopilot-code", "unit": "qa/code-review",
                "output_path": str(output),
            })
            row = (
                "2026-09-08T00:00:00Z\topen\t"
                f"{root}\t{root}\texecute\t"
                "attempt_schema_version=2,attempt_id=att-review,"
                "dispatch_depth=1,transport=headless,execution_surface=registered-headless,"
                "registered_worker=1,worker_type=review,unit=qa/code-review,"
                "capability=autopilot-code,artifact_root=" + str(root) +
                ",worktree=" + str(root) + ",review_cycle_id=cyc-1,"
                "review_producer_id=prod-1,review_output_locator_b64=" +
                D.encode_review_output_locator("campaigns/camp/cycle/artifacts/plans/report.md") +
                ",review_output_digest=" + digest + "\n"
            )
            jobs.write_text(row.replace("\\t", "\t").replace("\\n", "\n"), encoding="utf-8")
            binding = D.validate_review_output_binding(
                jobs, attempt_id="att-review", output_path=output,
                cycle_id="cyc-1", producer_id="prod-1", capability="autopilot-code",
                unit="qa/code-review", worktree=root, artifact_root=root,
            )
            self.assertEqual(binding["schema_version"], 2)
            self.assertTrue(binding["digest"].startswith("sha256:"))

    def test_binding_rejects_raw_output_metadata_and_sibling_target(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            jobs = root / "jobs.log"
            output = root / "reports" / "report.md"
            base = {
                "schema_version": 2, "attempt_id": "att-review", "cycle_id": "cyc-1",
                "producer_id": "prod-1", "worktree": str(root), "artifact_root": str(root),
                "capability": "autopilot-code", "unit": "qa/code-review",
                "output_path": str(output),
            }
            digest = D.review_output_binding_digest(base)
            row = (
                f"ts\topen\t{root}\t{root}\texecute\t"
                "attempt_schema_version=2,attempt_id=att-review,dispatch_depth=1,"
                "transport=headless,execution_surface=registered-headless,registered_worker=1,"
                "worker_type=review,unit=qa/code-review,capability=autopilot-code,"
                f"artifact_root={root},worktree={root},review_cycle_id=cyc-1,"
                f"review_producer_id=prod-1,review_output_digest={digest},review_output_path={output}\\n"
            )
            jobs.write_text(row.replace("\\t", "\t").replace("\\n", "\n"), encoding="utf-8")
            with self.assertRaises(D.DispatchContractError) as caught:
                D.validate_review_output_binding(
                    jobs, attempt_id="att-review", output_path=output,
                    cycle_id="cyc-1", producer_id="prod-1", capability="autopilot-code",
                    unit="qa/code-review", worktree=root, artifact_root=root,
                )
            self.assertEqual(caught.exception.reason, "review-output-path-in-jobs-metadata")

    def test_binding_rejects_legacy_schema_and_duplicate_metadata(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            jobs = root / "jobs.log"
            jobs.write_text(
                f"ts\topen\t{root}\t{root}\texecute\t"
                "attempt_schema_version=1,attempt_id=att-review,attempt_id=att-review,"
                "dispatch_depth=1,transport=headless,execution_surface=registered-headless,"
                "registered_worker=1,worker_type=review,unit=qa/code-review,"
                f"capability=autopilot-code,artifact_root={root},worktree={root},"
                "review_cycle_id=cyc-1,review_producer_id=prod-1,review_output_digest=x\n",
                encoding="utf-8",
            )
            with self.assertRaises(D.DispatchContractError) as caught:
                D.validate_review_output_binding(
                    jobs, attempt_id="att-review", output_path=root / "report.md",
                    cycle_id="cyc-1", producer_id="prod-1", capability="autopilot-code",
                    unit="qa/code-review", worktree=root, artifact_root=root,
                )
            self.assertEqual(caught.exception.reason, "review-attempt-row-malformed")

    def test_missing_or_foreign_binding_is_denied(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            jobs = root / "jobs.log"
            jobs.write_text("", encoding="utf-8")
            with self.assertRaises(D.DispatchContractError) as caught:
                D.validate_review_output_binding(
                    jobs, attempt_id="att-missing",
                    output_path=root / "foreign.md", cycle_id="cyc-1",
                    producer_id="prod-1", capability="autopilot-code",
                    unit="qa/code-review", worktree=root, artifact_root=root,
                )
            self.assertEqual(caught.exception.reason, "review-attempt-not-unique")

    def test_locator_encoding_carries_delimiters_without_raw_metadata(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            worktree = root / "worktree"
            worktree.mkdir()
            output = root / "plans" / "comma,tab\tline\nreport.md"
            row = self._row(
                repo=worktree, worktree=worktree, root=root, output=output,
            )
            metadata = row.split("\t", 5)[5]
            self.assertNotIn(str(output.relative_to(root)), metadata)
            jobs = root / "jobs.log"
            jobs.write_text(row, encoding="utf-8")
            self.assertEqual(
                self._validate(jobs, output, worktree, root)["output_path"],
                str(output),
            )

    def test_write_authorization_requires_live_governed_flock_and_exact_record(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            jobs = root / "jobs.log"
            output = root / "report.md"
            base = {
                "schema_version": 2, "attempt_id": "att-review", "cycle_id": "cyc-1",
                "producer_id": "prod-1", "worktree": str(root), "artifact_root": str(root),
                "capability": "autopilot-code", "unit": "qa/code-review", "output_path": str(output),
            }
            digest = D.review_output_binding_digest(base)
            identity = D.process_launch_identity(os.getpid())
            self.assertTrue(all(identity.get(key) for key in (
                "pid", "pid_start", "pgid", "pid_ns", "pid_observer_ns",
            )))
            nonce = "a" * 64
            acquired = datetime.now(timezone.utc) - timedelta(seconds=1)
            deadline = acquired + timedelta(minutes=5)
            stamp = lambda value: value.strftime("%Y-%m-%dT%H:%M:%SZ")
            lease = {
                "schema_version": 2, "attempt_id": "att-review", "cycle_id": "cyc-1",
                "producer_id": "prod-1", "worktree": str(root), "artifact_root": str(root),
                "capability": "autopilot-code", "unit": "qa/code-review",
                "review_output_path": str(output), "review_output_digest": digest,
                "acquired_at": stamp(acquired), "deadline": stamp(deadline),
                "released_at": None, "expired": False,
                "review_governed_lease": D.REVIEW_GOVERNED_LEASE_KIND,
                "review_governed_lease_nonce": nonce,
            }
            lease.update(identity)
            lease_digest = D.review_lease_record_digest(lease)
            jobs.write_text(self._row(
                repo=root, worktree=root, root=root, output=output,
                extra=(
                    f",review_governed_lease={D.REVIEW_GOVERNED_LEASE_KIND}"
                    f",review_governed_lease_nonce={nonce}"
                    f",review_lease_acquired_at={lease['acquired_at']}"
                    f",review_lease_deadline={lease['deadline']}"
                    f",review_lease_record_digest={lease_digest}"
                    + "".join(f",{key}={value}" for key, value in identity.items())
                ),
            ), encoding="utf-8")
            governed = D.review_governed_lease_path(root, "cyc-1", "att-review")
            governed.parent.mkdir(parents=True)
            governed.write_bytes(D.review_governed_lease_payload("att-review", "cyc-1", nonce))
            with governed.open("r+b") as held:
                fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.assertTrue(D.review_output_write_authorized(
                    jobs, output_path=output, attempt_id="att-review", cycle_id="cyc-1",
                    producer_id="prod-1", capability="autopilot-code", unit="qa/code-review",
                    worktree=root, artifact_root=root, lease_record=lease,
                ))
                mutations = (
                    {**lease, "released_at": lease["acquired_at"]},
                    {**lease, "expired": True},
                    {**lease, "deadline": "malformed"},
                    {key: value for key, value in lease.items() if key != "acquired_at"},
                    {**lease, "pid_start": "tampered"},
                )
                for changed in mutations:
                    with self.subTest(changed=changed):
                        self.assertFalse(D.review_output_write_authorized(
                            jobs, output_path=output, attempt_id="att-review",
                            cycle_id="cyc-1", producer_id="prod-1",
                            capability="autopilot-code", unit="qa/code-review",
                            worktree=root, artifact_root=root,
                            lease_record=changed,
                        ))
            self.assertTrue(D.review_output_write_authorized(
                jobs, output_path=output, attempt_id="att-review", cycle_id="cyc-1",
                producer_id="prod-1", capability="autopilot-code", unit="qa/code-review",
                worktree=root, artifact_root=root, lease_record=lease,
            ))


class ReviewOutputWrapperBoundaryTest(unittest.TestCase):
    MODELS = {
        "codex": ["--model", "gpt-test", "--reasoning", "low"],
        "claude": ["--model", "claude-test", "--effort", "low"],
        "opencode": ["--model", "provider/test", "--variant", "low"],
    }

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.worktree = self.base / "repo"
        self.worktree.mkdir()
        subprocess.run(["git", "init", "-q", str(self.worktree)], check=True)
        self.root = self.base / ".agent_reports"
        self.root.mkdir()
        P.activate(
            self.root, repository_id="repo_" + "a" * 32,
            artifact_root_id="root_" + "b" * 32,
        )
        gate = {
            "spec_read": {"satisfied": True, "source": "fixture"},
            "drift_verdict": "within-spec", "workflow_mode": "tracked",
            "artifact_guard": {"satisfied": True, "source": "fixture"},
        }
        route = R.compile_route(
            "autopilot-code", "debug", "direct", cwd=self.worktree,
            artifact_root=self.root, tracking="tracked",
            tracked_gate_evidence=gate, slug="review-wrapper-boundary",
            predicates=[
                "atomic-outcome", "known-scope", "no-shared-contract",
                "no-resource-run", "no-artifact-handoff",
                "no-independent-verifier", "focused-verification",
            ], transport=None, inline_reason="atomic-direct",
        )
        route_binding = L.admit_runtime_route(self.root, route)
        self.route = route
        self.route_file = Path(route_binding.route_file)
        self.cycle = P.begin(
            self.root, route_file=self.route_file,
            capability="autopilot-code", intensity="direct",
        )
        (self.base / "home").mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def _wrapper(self, harness):
        spec = importlib.util.spec_from_file_location(
            f"review_output_{harness}_{id(self)}",
            PROJECT_ROOT / f"adapters/{harness}/bin/dispatch-headless.py",
        )
        wrapper = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(wrapper)
        return wrapper

    def _run(self, harness, *, attempt, output=None, worker_type="review",
             route_args=(), with_cycle=True):
        jobs = self.base / f"{harness}-{attempt}.jobs.log"
        jobs.write_text("", encoding="utf-8")
        argv = [
            "dispatch-headless.py", "--register", "--worktree", str(self.worktree),
            "--slug", f"review-{harness}-{attempt}", "--capability", "autopilot-code",
            "--capability-mode", "debug", "--intensity", "standard",
            "--dispatch-depth", "1", "--worker-type", worker_type,
            "--unit", "qa/code-review" if worker_type == "review" else "_kernel/owner",
            "--assigned-contract", "autopilot-code", "--owner", "review-owner",
            "--owner-harness", "codex", "--jobs", str(jobs),
            "--attempt-id", attempt, *route_args, *self.MODELS[harness],
        ]
        if output is not None:
            argv += ["--review-output", str(output)]
        env = {
            "PATH": os.environ.get("PATH", ""), "HOME": str(self.base / "home"),
            "AGENT_HOME": str(PROJECT_ROOT), "AGENT_ARTIFACT_ROOT": str(self.root),
            "AGENT_DISPATCH_JOBS": str(jobs), "AGENT_DISPATCH_CHILD": "1",
            "AGENT_DISPATCH_ATTEMPT_ID": "att-parent-fixture",
            "OPENCODE_CONFIG_CONTENT": "{}", "XDG_STATE_HOME": str(self.base / "state"),
        }
        if with_cycle:
            env.update(self.cycle["env"])
        stream = StringIO()
        wrapper = self._wrapper(harness)
        with mock.patch.dict(os.environ, env, clear=True), redirect_stdout(stream):
            code = wrapper.main(argv)
        return code, stream.getvalue(), jobs

    @contextmanager
    def _live_review_authority(self, *, attempt, output):
        binding = P.prepare_review_output_binding(
            self.root, cycle_id=self.cycle["cycle_id"],
            producer_id=self.cycle["producer_id"], attempt_id=attempt,
            review_output=output, capability="autopilot-code",
            unit="qa/code-review", worktree=self.worktree,
        )
        identity = D.process_launch_identity(os.getpid())
        nonce = "e" * 64
        now = datetime.now(timezone.utc)
        stamp = lambda value: value.strftime("%Y-%m-%dT%H:%M:%SZ")
        record = {
            "schema_version": 2, "attempt_id": attempt,
            "cycle_id": self.cycle["cycle_id"],
            "producer_id": self.cycle["producer_id"],
            "worktree": str(self.worktree), "artifact_root": str(self.root),
            "capability": "autopilot-code", "unit": "qa/code-review",
            "review_output_path": str(output),
            "review_output_digest": binding["digest"],
            "acquired_at": stamp(now - timedelta(seconds=1)),
            "deadline": stamp(now + timedelta(minutes=5)),
            "released_at": None, "expired": False,
            "review_governed_lease": D.REVIEW_GOVERNED_LEASE_KIND,
            "review_governed_lease_nonce": nonce,
        }
        record.update({
            key: int(value) if key in {"pid", "pgid"} else value
            for key, value in identity.items()
        })
        record_digest = D.review_lease_record_digest(record)
        metadata = (
            "attempt_schema_version=2,dispatch_depth=1,transport=headless,"
            "execution_surface=registered-headless,registered_worker=1,"
            "worker_type=review,unit=qa/code-review,capability=autopilot-code,"
            f"attempt_id={attempt},artifact_root={self.root},"
            f"review_cycle_id={self.cycle['cycle_id']},"
            f"review_producer_id={self.cycle['producer_id']},"
            f"review_output_locator_b64={binding['locator_b64']},"
            f"review_output_digest={binding['digest']},"
            f"review_governed_lease={D.REVIEW_GOVERNED_LEASE_KIND},"
            f"review_governed_lease_nonce={nonce},"
            f"review_lease_acquired_at={record['acquired_at']},"
            f"review_lease_deadline={record['deadline']},"
            f"review_lease_record_digest={record_digest}"
            + "".join(f",{key}={value}" for key, value in identity.items())
        )
        jobs = self.base / f"{attempt}.jobs.log"
        jobs.write_text(
            f"ts\topen\t{self.worktree}\t{self.worktree}\treview\t{metadata}\n",
            encoding="utf-8",
        )
        lease_path = P._review_lease_path(
            self.root, self.cycle["cycle_id"], attempt
        )
        lease_path.parent.mkdir(parents=True, exist_ok=True)
        lease_path.write_text(json.dumps(record), encoding="utf-8")
        governed = D.review_governed_lease_path(
            self.root, self.cycle["cycle_id"], attempt
        )
        governed.parent.mkdir(parents=True, exist_ok=True)
        governed.write_bytes(D.review_governed_lease_payload(
            attempt, self.cycle["cycle_id"], nonce
        ))
        env = {
            "PATH": os.environ.get("PATH", ""), "HOME": str(self.base / "home"),
            "AGENT_HOME": str(PROJECT_ROOT), "AGENT_ARTIFACT_ROOT": str(self.root),
            "AGENT_DISPATCH_JOBS": str(jobs),
            "AGENT_DISPATCH_ATTEMPT_ID": attempt,
            "AGENT_DISPATCH_WORKTREE": str(self.worktree),
            "AGENT_DISPATCH_UNIT": "qa/code-review",
            "AGENT_REVIEW_CYCLE_ID": self.cycle["cycle_id"],
            "AGENT_REVIEW_PRODUCER_ID": self.cycle["producer_id"],
            "AGENT_REVIEW_OUTPUT": str(output), **self.cycle["env"],
        }
        with governed.open("r+b") as held:
            fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield env

    def test_all_wrappers_register_exact_report_with_path_safe_metadata(self):
        output = Path(self.cycle["cycle_dir"]) / "artifacts/plans/comma,tab\tline\n.md"
        for index, harness in enumerate(self.MODELS):
            with self.subTest(harness=harness):
                code, stdout, jobs = self._run(
                    harness, attempt=f"att-review-valid-{index}", output=output,
                )
                self.assertEqual(code, 0, stdout)
                row = jobs.read_text(encoding="utf-8")
                self.assertIn("review_output_locator_b64=", row)
                self.assertNotIn("review_output_locator=", row)
                self.assertNotIn(str(output.relative_to(self.root)), row)

    def test_stdout_only_review_remains_registered_without_cycle_binding(self):
        for index, harness in enumerate(self.MODELS):
            with self.subTest(harness=harness):
                code, stdout, jobs = self._run(
                    harness, attempt=f"att-review-stdout-{index}", with_cycle=False,
                )
                self.assertEqual(code, 0, stdout)
                self.assertEqual(len(jobs.read_text().splitlines()), 1)
                self.assertNotIn("review_output_", jobs.read_text())

    def test_foreign_and_nonreview_reports_fail_before_registry_mutation(self):
        foreign = self.base / "foreign" / "report.md"
        for index, harness in enumerate(self.MODELS):
            for label, output, worker_type in (
                ("foreign", foreign, "review"),
                ("owner", Path(self.cycle["cycle_dir"]) / "artifacts/plans/owner.md", "owner"),
            ):
                with self.subTest(harness=harness, label=label):
                    code, stdout, jobs = self._run(
                        harness, attempt=f"att-review-deny-{index}-{label}",
                        output=output, worker_type=worker_type,
                    )
                    self.assertEqual(code, 65, stdout)
                    self.assertIn("registry_mutation=0", stdout)
                    self.assertIn("child_spawned=0", stdout)
                    self.assertEqual(jobs.read_text(), "")

    def test_closed_cycle_and_route_bound_report_are_typed_preclaim_denials(self):
        output = Path(self.cycle["cycle_dir"]) / "artifacts/plans/closed.md"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text("evidence\n", encoding="utf-8")
        P.finalize(
            self.root, cycle_id=self.cycle["cycle_id"], state="abandoned",
            abandon_reason="operator-decision", allow_open_route=True,
        )
        node = self.route["nodes"][0]
        route_args = (
            "--route-file", str(self.route_file),
            "--route-id", self.route["route_id"],
            "--route-hash", self.route["route_hash"],
            "--route-node", node["id"],
            "--registry-digest", self.route["registry_digest"],
            "--write-scope", node["write_scope"],
        )
        for index, harness in enumerate(self.MODELS):
            with self.subTest(harness=harness, case="closed"):
                code, stdout, jobs = self._run(
                    harness, attempt=f"att-review-closed-{index}", output=output,
                )
                self.assertEqual(code, 65, stdout)
                self.assertIn("reason=cycle-not-open", stdout)
                self.assertEqual(jobs.read_text(), "")
            with self.subTest(harness=harness, case="route-bound"):
                code, stdout, jobs = self._run(
                    harness, attempt=f"att-review-route-{index}", output=output,
                    route_args=route_args,
                )
                self.assertEqual(code, 65, stdout)
                self.assertIn("child_spawned=0", stdout)
                self.assertEqual(jobs.read_text(), "")

    def test_candidate_public_preflight_creates_only_the_exact_live_report(self):
        attempt = "att-review-public-write"
        output = Path(self.cycle["cycle_dir"]) / "artifacts/plans/public-report.md"
        output.parent.mkdir(parents=True, exist_ok=True)
        preflight = PROJECT_ROOT / "adapters/codex/bin/preflight.sh"
        with self._live_review_authority(attempt=attempt, output=output) as env:
            created = subprocess.run(
                [
                    "sh", "-c",
                    '"$1" write "$2" "$3" && printf "%s\\n" verified > "$2"',
                    "review-write", str(preflight), str(output), attempt,
                ],
                text=True, capture_output=True, env=env,
            )
            self.assertEqual(created.returncode, 0, created.stdout + created.stderr)
            self.assertEqual(output.read_text(), "verified\n")
            for denied in (
                Path(self.cycle["cycle_dir"]) / "artifacts/_internal/foreign.md",
                self.base / "foreign-root" / "report.md",
                self.worktree / "source.py",
            ):
                before = denied.exists()
                result = subprocess.run(
                    [str(preflight), "write", str(denied), attempt],
                    text=True, capture_output=True, env=env,
                )
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(denied.exists(), before)

    def test_public_preflight_uses_governed_flock_across_pid_namespace(self):
        bwrap = shutil.which("bwrap")
        if not bwrap:
            self.skipTest("bubblewrap is unavailable")
        base = [
            bwrap, "--die-with-parent", "--unshare-pid", "--ro-bind", "/", "/",
            "--proc", "/proc", "--dev", "/dev", "--bind", str(self.root),
            str(self.root),
        ]
        probe = subprocess.run([*base, "true"], text=True, capture_output=True)
        if probe.returncode:
            self.skipTest("bubblewrap PID namespace unavailable: " + probe.stderr.strip())
        attempt = "att-review-bwrap-write"
        output = Path(self.cycle["cycle_dir"]) / "artifacts/plans/bwrap-report.md"
        output.parent.mkdir(parents=True, exist_ok=True)
        preflight = PROJECT_ROOT / "adapters/codex/bin/preflight.sh"
        with self._live_review_authority(attempt=attempt, output=output) as env:
            outer_pid = str(os.getpid())
            outer_namespace = os.readlink("/proc/self/ns/pid")
            self.assertIn(f",pid={outer_pid}", Path(env["AGENT_DISPATCH_JOBS"]).read_text())
            created = subprocess.run(
                [
                    *base, "sh", "-c",
                    'test "$(readlink /proc/self/ns/pid)" != "$5" '
                    '&& "$1" write "$2" "$3" '
                    '&& printf "%s\\n" namespace-verified > "$2"',
                    "review-write", str(preflight), str(output), attempt,
                    outer_pid, outer_namespace,
                ],
                text=True, capture_output=True, env=env,
            )
            self.assertEqual(created.returncode, 0, created.stdout + created.stderr)
            self.assertEqual(output.read_text(), "namespace-verified\n")


class ReviewAdapterWiringParityTest(unittest.TestCase):
    """The three wrappers consume the same budget/admission contract."""

    HARNESSES = ("claude", "codex", "opencode")

    def _wrapper(self, harness):
        spec = importlib.util.spec_from_file_location(
            f"review_wiring_{harness}_{id(self)}",
            PROJECT_ROOT / f"adapters/{harness}/bin/dispatch-headless.py",
        )
        wrapper = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(wrapper)
        return wrapper

    def _args(self, lifecycle):
        return SimpleNamespace(
            review_output="/tmp/review-output.md",
            review_output_binding={
                "cycle_id": "cyc-review", "output_path": "/tmp/review-output.md",
            },
            watchdog_budget=DL.begin_finite_watchdog(
                12.0, origin_monotonic_ns=10_000, origin_epoch=100.0
            ),
            artifact_root=Path("/tmp/review-artifact-root"),
            attempt_id="att-review-wiring", review_governed_lease_nonce="a" * 64,
            launch_lifecycle=lifecycle, review_watchdog_handle=object(),
        )

    def test_each_adapter_routes_detached_and_foreground_to_shared_admission(self):
        identity = {
            "pid": "11", "pid_start": "22", "pgid": "11",
            "pid_ns": "pid:[1]", "pid_observer_ns": "pid:[1]",
        }
        for harness in self.HARNESSES:
            wrapper = self._wrapper(harness)
            with self.subTest(harness=harness, lifecycle="detached"):
                args = self._args(DL.DETACHED)
                with mock.patch.object(wrapper, "acquire_review_admission", return_value="detached") as acquire:
                    self.assertEqual(
                        wrapper.acquire_review_lease_after_claim(args, Path("/tmp/jobs"), identity),
                        "detached",
                    )
                kwargs = acquire.call_args.kwargs
                self.assertIs(kwargs["budget"], args.watchdog_budget)
                self.assertIs(kwargs["handle"], args.review_watchdog_handle)
                self.assertEqual(kwargs["nonce"], args.review_governed_lease_nonce)
                self.assertIs(kwargs["lease_acquire"], wrapper.review_lease_acquire)
                with self.subTest(probe="witness-unlocked"):
                    with mock.patch.object(wrapper, "review_governed_lease_is_held", return_value=True):
                        self.assertFalse(kwargs["witness_probe"]())

            with self.subTest(harness=harness, lifecycle="foreground"):
                args = self._args(DL.FOREGROUND_SCOPED)
                with mock.patch.object(wrapper, "acquire_foreground_review_admission", return_value="foreground") as acquire:
                    self.assertEqual(
                        wrapper.acquire_review_lease_after_claim(args, Path("/tmp/jobs"), identity),
                        "foreground",
                    )
                kwargs = acquire.call_args.kwargs
                self.assertIs(kwargs["budget"], args.watchdog_budget)
                self.assertEqual(kwargs["identity"], identity)
                self.assertIs(kwargs["lease_acquire"], wrapper.review_lease_acquire)

    def test_each_adapter_has_watchdog_spawn_and_absolute_foreground_budget(self):
        for harness in self.HARNESSES:
            path = PROJECT_ROOT / f"adapters/{harness}/bin/dispatch-headless.py"
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            spawn_nodes = [
                node for node in ast.walk(tree)
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == "spawn_worker"
            ]
            self.assertEqual(len(spawn_nodes), 1, harness)
            spawn_names = {
                node.func.id for node in ast.walk(spawn_nodes[0])
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            }
            self.assertIn("launch_review_watchdog", spawn_names, harness)
            all_calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]
            names = {
                node.func.id for node in all_calls if isinstance(node.func, ast.Name)
            }
            self.assertIn("begin_finite_watchdog", names, harness)
            wait_calls = [
                node for node in all_calls
                if isinstance(node.func, ast.Name) and node.func.id == "wait_foreground"
            ]
            self.assertTrue(
                any(any(keyword.arg == "watchdog_budget" for keyword in call.keywords)
                    for call in wait_calls),
                harness,
            )

    def test_foreground_admission_proves_direct_group_and_external_cleanup(self):
        with tempfile.TemporaryDirectory() as td:
            child = subprocess.Popen(["sleep", "0.05"], start_new_session=True)
            identity = D.process_launch_identity(child.pid)
            child.wait(timeout=2)
            released = []
            budget = DL.begin_finite_watchdog(
                12.0, origin_monotonic_ns=10_000, origin_epoch=100.0
            )
            admission = DL.acquire_foreground_review_admission(
                budget=budget, identity=identity, root=Path(td),
                cycle_id="cyc-review", attempt_id="att-review-direct",
                review_output=Path(td) / "report.md", binding={"binding": "fixture"},
                jobs=Path(td) / "jobs.log",
                lease_acquire=lambda *_args, **kwargs: {
                    "status": "acquired",
                    "registry_metadata": {
                        "review_lease_acquired_at": "2026-01-01T00:00:00Z",
                        "review_lease_deadline": "2026-01-01T00:01:00Z",
                        "review_lease_record_digest": "sha256:" + "a" * 64,
                    },
                },
                lease_release=lambda: (released.append(True) or {"status": "released"}),
                witness_probe=lambda: True,
            )
            cleanup = admission.abort("direct-group-fault")
            self.assertEqual(cleanup.status, "verified-never-launched")
            self.assertEqual(cleanup.fenced_child_group, "empty")
            self.assertEqual(cleanup.review_lease, "released")
            self.assertEqual(released, [True])

    def test_foreground_admission_keeps_lease_for_live_or_unobservable_process_set(self):
        with tempfile.TemporaryDirectory() as td:
            child = subprocess.Popen(["sleep", "30"], start_new_session=True)
            identity = D.process_launch_identity(child.pid)
            try:
                for namespace in (identity["pid_observer_ns"], "pid:[foreign]"):
                    release = mock.Mock(return_value={"status": "released"})
                    admission = DL.acquire_foreground_review_admission(
                        budget=DL.begin_finite_watchdog(30),
                        identity={**identity, "pid_observer_ns": namespace}, root=Path(td),
                        cycle_id="cyc-live", attempt_id="att-live-review",
                        review_output=Path(td)/"report.md", binding={}, jobs=Path(td)/"jobs.log",
                        lease_acquire=lambda *_a, **_k: {"status": "acquired", "registry_metadata": {}},
                        lease_release=release, witness_probe=lambda: True)
                    self.assertEqual(admission.abort("fault").status, "unverified")
                    release.assert_not_called()
            finally:
                child.terminate(); child.wait(timeout=5)


class PreflightHelpBoundaryTest(unittest.TestCase):
    def test_codex_and_opencode_write_help_and_argv_boundary(self):
        with tempfile.TemporaryDirectory() as td:
            target = str(Path(td).resolve() / "literal")
            for adapter in ("codex", "opencode"):
                script = PROJECT_ROOT / f"adapters/{adapter}/bin/preflight.sh"
                for flag in ("--help", "-h"):
                    with self.subTest(adapter=adapter, flag=flag):
                        result = subprocess.run(
                            [str(script), "write", flag], text=True,
                            capture_output=True, cwd=td,
                        )
                        self.assertEqual(result.returncode, 0, result.stderr)
                        self.assertIn("usage: preflight.sh write", result.stdout)
                too_many = subprocess.run(
                    [str(script), "write", target, "sid", "turn", "extra"],
                    text=True, capture_output=True, cwd=td,
                )
                self.assertEqual(too_many.returncode, 64)
                self.assertIn("write expects", too_many.stderr)
                dash_path = subprocess.run(
                    [str(script), "write", "--literal-not-help"],
                    text=True, capture_output=True, cwd=td,
                )
                self.assertEqual(dash_path.returncode, 64)
                self.assertIn("must be absolute or ./-prefixed", dash_path.stderr)
        self.assertFalse(
            (PROJECT_ROOT / "adapters/claude/bin/preflight.sh").exists(),
            "Codex/OpenCode preflight help must not be projected into Claude",
        )
if __name__ == "__main__":
    unittest.main()
