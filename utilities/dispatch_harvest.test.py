#!/usr/bin/env python3
"""Exact-attempt harvest regression tests across registered headless adapters."""

import importlib.util
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "capability_route_harvest", ROOT / "utilities/capability-route.py"
)
ROUTE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ROUTE)


class HarvestTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.repo = self.base / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        subprocess.run(
            ["git", "-C", str(self.repo), "config", "user.email", "fixture@example.com"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(self.repo), "config", "user.name", "Fixture"],
            check=True,
        )
        (self.repo / "x").write_text("x\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.repo), "add", "x"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-qm", "init"], check=True)
        self.artifact = self.base / ".agent_reports"
        self.artifact.mkdir()
        self.home = self.base / "agent-home"
        (self.home / "core").mkdir(parents=True)
        (self.home / "core/CORE.md").write_text("fixture\n", encoding="utf-8")
        gate = {
            "spec_read": {"satisfied": True, "source": "fixture"},
            "drift_verdict": "within-spec",
            "workflow_mode": "tracked",
            "artifact_guard": {"satisfied": True, "source": "fixture"},
        }
        dispatch = {
            "tuples": [{
                "parent_harness": "codex",
                "parent_transport": "headless",
                "parent_sandbox": "workspace-write",
                "child_harness": "codex",
                "launch_authority": "conductor",
                "status": "supported",
                "probe_source": "fixture",
                "probe_time": "2026-07-21T00:00:00Z",
                "failure_class": "",
                "checked_worktree": str(self.repo.resolve()),
                "failure_scope": "none",
                "codex_command": "ok",
                "retry_on_isolated_worktree": 0,
            }],
            "native_subagent": [],
        }
        self.route = ROUTE.compile_route(
            "autopilot-code", "dev", "strong", self.repo, self.artifact,
            signals=["shared-contract"], transport="headless",
            tracking="tracked", tracked_gate_evidence=gate,
            dispatch_evidence=dispatch,
        )
        self.route_path = self.base / "route.json"
        self.route_path.write_text(json.dumps(self.route), encoding="utf-8")
        self.node = next(node for node in self.route["nodes"] if node["id"] == "plan")
        self.evidence = self.base / "plan.md"
        self.evidence.write_text("plan\n", encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def env(self):
        return {
            **os.environ,
            "AGENT_HOME": str(self.home),
            "AGENT_ARTIFACT_ROOT": str(self.artifact),
        }

    def current_row(self, attempt, slug="worker"):
        pipe = (
            "attempt_schema_version=2,dispatch_depth=2,transport=headless,"
            "execution_surface=registered-headless,registered_worker=1,"
            "fallback_hop=same-harness-headless,"
            f"attempt_id={attempt},route_id={self.route['route_id']},"
            f"route_hash={self.route['route_hash']},route_node=plan,"
            f"route_file={self.route_path},completion_gate={self.node['completion_gate']}"
        )
        return (
            f"2026-07-21T00:00:00Z\topen\t{self.repo}\t{self.repo}\t{slug}\t{pipe}\n"
        )

    def terminal_log(self, verdict, blocker, artifact="-", diagnostic=None, name=None):
        path = self.base / (name or f"{verdict.lower()}.codex.jsonl")
        rows = []
        if diagnostic is not None:
            rows.append({
                "type": "item.completed",
                "item": {"type": "command_execution", "exit_code": 1,
                         "aggregated_output": diagnostic},
            })
        rows.extend([
            {"type": "item.completed", "item": {"type": "agent_message",
             "text": f"artifact: {artifact}\nverdict: {verdict}\nblocker: {blocker}"}},
            {"type": "turn.completed"},
        ])
        path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
        return path

    def terminal_row(self, attempt, verdict, blocker, *, status="open", slug="worker",
                     artifact="-", diagnostic=None, name=None):
        log = self.terminal_log(verdict, blocker, artifact, diagnostic, name)
        row = self.current_row(attempt, slug).replace("\topen\t", f"\t{status}\t")
        return row.rstrip("\n") + (
            f",harness=codex,artifact_root={self.artifact},log_file={log}\n"
        )

    def claude_terminal_row(self, attempt, verdict, blocker, *, status="open"):
        log = self.base / f"{verdict.lower()}.claude.jsonl"
        result = f"artifact: -\nverdict: {verdict}\nblocker: {blocker}"
        log.write_text(json.dumps({"type": "system", "subtype": "init"}) + "\n" +
                       json.dumps({"type": "result", "subtype": "success",
                                   "result": result}) + "\n")
        row = self.current_row(attempt, "claude-worker").replace(
            "\topen\t", f"\t{status}\t"
        )
        return row.rstrip("\n") + (
            f",harness=claude,artifact_root={self.artifact},log_file={log}\n"
        )

    def test_routed_harvest_replays_shared_completion_for_one_exact_attempt(self):
        attempt = "att-harvest-exact"
        old = os.environ.get("AGENT_HOME")
        os.environ["AGENT_HOME"] = str(self.home)
        try:
            with self.assertRaisesRegex(ValueError, "attempt-row-absent"):
                ROUTE.complete_node(
                    self.route, self.node, "plan", self.evidence,
                    jobs=self.base / "missing.jobs.log", attempt_id=attempt,
                    explicit_attempt_metadata={
                        "attempt_schema_version": 2,
                        "dispatch_depth": 2,
                        "transport": "headless",
                        "execution_surface": "registered-headless",
                        "registered_worker": True,
                        "fallback_hop": "same-harness-headless",
                    },
                )
        finally:
            if old is None:
                os.environ.pop("AGENT_HOME", None)
            else:
                os.environ["AGENT_HOME"] = old
        completion = (
            self.home / ".dispatch/completion" / self.route["route_id"] / "plan.json"
        )
        self.assertTrue(completion.is_file())

        for adapter in ("codex", "opencode"):
            with self.subTest(adapter=adapter):
                jobs = self.base / f"{adapter}.jobs.log"
                jobs.write_text(self.current_row(attempt), encoding="utf-8")
                result = subprocess.run(
                    [
                        sys.executable,
                        str(ROOT / f"adapters/{adapter}/bin/dispatch-harvest.py"),
                        "--jobs", str(jobs), "--slug", "worker", "--status", "open",
                        "--mark-done", "--completion", str(completion),
                    ],
                    text=True, capture_output=True, env=self.env(),
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                row = jobs.read_text(encoding="utf-8")
                self.assertIn("\tdone\t", row)
                self.assertIn("note=completed-marker", row)

    # -- Phase 5 (plan.md, round_1 finding 5): derived route completion -----

    def test_derived_completion_succeeds_with_no_completion_flag(self):
        for adapter in ("codex", "opencode"):
            with self.subTest(adapter=adapter):
                attempt = f"att-derived-pass-{adapter}"
                artifact = self.artifact / f"derived-{adapter}.md"
                artifact.write_text("evidence\n", encoding="utf-8")
                jobs = self.base / f"{adapter}.derived.jobs.log"
                row = self.terminal_row(
                    attempt, "PASS", "none", artifact=str(artifact),
                    name=f"derived-{adapter}.codex.jsonl",
                )
                jobs.write_text(row, encoding="utf-8")
                result = subprocess.run(
                    [
                        sys.executable,
                        str(ROOT / f"adapters/{adapter}/bin/dispatch-harvest.py"),
                        "--jobs", str(jobs), "--slug", "worker", "--status", "open",
                        "--mark-done",
                    ],
                    text=True, capture_output=True, env=self.env(),
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                after = jobs.read_text(encoding="utf-8")
                self.assertIn("\tdone\t", after)
                self.assertIn("note=completed-marker", after)

    def test_derived_completion_still_refused_for_blocked_verdict(self):
        for adapter in ("codex", "opencode"):
            with self.subTest(adapter=adapter):
                attempt = f"att-derived-blocked-{adapter}"
                artifact = self.artifact / f"derived-blocked-{adapter}.md"
                artifact.write_text("evidence\n", encoding="utf-8")
                jobs = self.base / f"{adapter}.derived-blocked.jobs.log"
                row = self.terminal_row(
                    attempt, "BLOCKED", "stuck", artifact=str(artifact),
                    name=f"derived-blocked-{adapter}.codex.jsonl",
                )
                before = row
                jobs.write_text(row, encoding="utf-8")
                result = subprocess.run(
                    [
                        sys.executable,
                        str(ROOT / f"adapters/{adapter}/bin/dispatch-harvest.py"),
                        "--jobs", str(jobs), "--slug", "worker", "--status", "open",
                        "--mark-done",
                    ],
                    text=True, capture_output=True, env=self.env(),
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("route-completion-required", result.stdout)
                self.assertEqual(jobs.read_text(encoding="utf-8"), before)

    def test_derived_completion_refused_for_out_of_root_artifact(self):
        for adapter in ("codex", "opencode"):
            with self.subTest(adapter=adapter):
                attempt = f"att-derived-outroot-{adapter}"
                outside = self.base / f"outside-{adapter}.md"
                outside.write_text("evidence\n", encoding="utf-8")
                jobs = self.base / f"{adapter}.derived-outroot.jobs.log"
                row = self.terminal_row(
                    attempt, "PASS", "none", artifact=str(outside),
                    name=f"derived-outroot-{adapter}.codex.jsonl",
                )
                before = row
                jobs.write_text(row, encoding="utf-8")
                result = subprocess.run(
                    [
                        sys.executable,
                        str(ROOT / f"adapters/{adapter}/bin/dispatch-harvest.py"),
                        "--jobs", str(jobs), "--slug", "worker", "--status", "open",
                        "--mark-done",
                    ],
                    text=True, capture_output=True, env=self.env(),
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("route-completion-required", result.stdout)
                self.assertEqual(jobs.read_text(encoding="utf-8"), before)

    def test_explicit_completion_missing_file_is_hard_error(self):
        # Advisory (round 2, reviewer A #3): an explicit --completion that is
        # not a readable file must hard-error, not silently fall through to
        # the derived path — a typo'd or since-deleted marker path should not
        # be masked by an unrelated derived PASS succeeding instead.
        for adapter in ("codex", "opencode"):
            with self.subTest(adapter=adapter):
                attempt = f"att-explicit-missing-{adapter}"
                artifact = self.artifact / f"explicit-missing-{adapter}.md"
                artifact.write_text("evidence\n", encoding="utf-8")
                jobs = self.base / f"{adapter}.explicit-missing.jobs.log"
                row = self.terminal_row(
                    attempt, "PASS", "none", artifact=str(artifact),
                    name=f"explicit-missing-{adapter}.codex.jsonl",
                )
                before = row
                jobs.write_text(row, encoding="utf-8")
                missing_completion = self.base / f"does-not-exist-{adapter}.json"
                result = subprocess.run(
                    [
                        sys.executable,
                        str(ROOT / f"adapters/{adapter}/bin/dispatch-harvest.py"),
                        "--jobs", str(jobs), "--slug", "worker", "--status", "open",
                        "--mark-done", "--completion", str(missing_completion),
                    ],
                    text=True, capture_output=True, env=self.env(),
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("route-completion-required", result.stdout)
                self.assertEqual(jobs.read_text(encoding="utf-8"), before)

    def test_stale_explicit_completion_still_refused(self):
        attempt = "att-harvest-stale"
        old = os.environ.get("AGENT_HOME")
        os.environ["AGENT_HOME"] = str(self.home)
        try:
            with self.assertRaisesRegex(ValueError, "attempt-row-absent"):
                ROUTE.complete_node(
                    self.route, self.node, "plan", self.evidence,
                    jobs=self.base / "stale-missing.jobs.log",
                    attempt_id="att-other-attempt",
                    explicit_attempt_metadata={
                        "attempt_schema_version": 2,
                        "dispatch_depth": 2,
                        "transport": "headless",
                        "execution_surface": "registered-headless",
                        "registered_worker": True,
                        "fallback_hop": "same-harness-headless",
                    },
                )
        finally:
            if old is None:
                os.environ.pop("AGENT_HOME", None)
            else:
                os.environ["AGENT_HOME"] = old
        # A completion marker minted for a DIFFERENT attempt id — reused
        # against this row it must fail the identity check, not complete it.
        completion = (
            self.home / ".dispatch/completion" / self.route["route_id"] / "plan.json"
        )
        self.assertTrue(completion.is_file())
        jobs = self.base / "stale.jobs.log"
        jobs.write_text(self.current_row(attempt), encoding="utf-8")
        before = jobs.read_text(encoding="utf-8")
        result = subprocess.run(
            [
                sys.executable,
                str(ROOT / "adapters/codex/bin/dispatch-harvest.py"),
                "--jobs", str(jobs), "--slug", "worker", "--status", "open",
                "--mark-done", "--completion", str(completion),
            ],
            text=True, capture_output=True, env=self.env(),
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("stale-route-completion", result.stdout)
        self.assertEqual(jobs.read_text(encoding="utf-8"), before)

    def test_sibling_row_stays_byte_identical_after_derived_harvest(self):
        attempt = f"att-derived-sibling-target"
        artifact = self.artifact / "derived-sibling.md"
        artifact.write_text("evidence\n", encoding="utf-8")
        target_row = self.terminal_row(
            attempt, "PASS", "none", artifact=str(artifact),
            name="derived-sibling.codex.jsonl",
        )
        sibling_row = self.current_row("att-derived-sibling-other", "other-worker")
        jobs = self.base / "codex.derived-sibling.jobs.log"
        jobs.write_text(sibling_row + target_row, encoding="utf-8")
        result = subprocess.run(
            [
                sys.executable,
                str(ROOT / "adapters/codex/bin/dispatch-harvest.py"),
                "--jobs", str(jobs), "--slug", "worker", "--status", "open",
                "--mark-done",
            ],
            text=True, capture_output=True, env=self.env(),
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        after_lines = jobs.read_text(encoding="utf-8").splitlines()
        self.assertIn(sibling_row.strip(), after_lines, "sibling row must stay untouched (SD-70)")

    def test_native_stop_done_harvest_consumes_exact_delivered_receipt(self):
        attempt = "att-native-stop-harvest"
        session = "thread-native-stop-harvest"
        jobs = self.base / "native-stop.jobs.log"
        row = self.terminal_row(
            attempt,
            "PASS",
            "none",
            status="done",
            name="native-stop.codex.jsonl",
        )
        row = row.replace("dispatch_depth=2", "dispatch_depth=1").rstrip("\n")
        row += (
            ",parent_completion_delivery=codex-stop-hook,"
            f"parent_sid={session}\n"
        )
        jobs.write_text(row, encoding="utf-8")
        digest = hashlib.sha256(session.encode("utf-8")).hexdigest()
        state = jobs.parent / "parent-session-state" / f"{digest}.json"
        state.parent.mkdir(parents=True, exist_ok=True)
        state.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "parent_session_id_sha256": digest,
                    "attempt_ids": [attempt],
                    "delivered_attempt_ids": [attempt],
                }
            ),
            encoding="utf-8",
        )
        status_done = subprocess.run(
            [
                sys.executable,
                str(ROOT / "adapters/codex/bin/dispatch-harvest.py"),
                "--jobs",
                str(jobs),
                "--attempt-id",
                attempt,
                "--status",
                "done",
            ],
            text=True,
            capture_output=True,
            env=self.env(),
        )
        self.assertEqual(
            status_done.returncode, 0, status_done.stdout + status_done.stderr
        )
        self.assertNotIn("parent_completion_receipt=consumed", status_done.stdout)
        self.assertTrue(state.exists())
        self.assertNotIn(
            "parent_completion_harvested=1",
            jobs.read_text(encoding="utf-8"),
        )

        result = subprocess.run(
            [
                sys.executable,
                str(ROOT / "adapters/codex/bin/dispatch-harvest.py"),
                "--jobs",
                str(jobs),
                "--attempt-id",
                attempt,
                "--status",
                "all",
            ],
            text=True,
            capture_output=True,
            env=self.env(),
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("parent_completion_receipt=consumed", result.stdout)
        self.assertFalse(state.exists())
        self.assertIn(
            "parent_completion_harvested=1",
            jobs.read_text(encoding="utf-8"),
        )

    def test_native_stop_runtime_failure_still_consumes_delivered_receipt(self):
        attempt = "att-native-stop-runtime-failure"
        session = "thread-native-stop-runtime-failure"
        jobs = self.base / "native-stop-runtime-failure.jobs.log"
        missing_log = self.base / "missing-runtime-failure.codex.jsonl"
        row = self.current_row(attempt).replace("\topen\t", "\tdone\t")
        row = row.replace("dispatch_depth=2", "dispatch_depth=1").rstrip("\n")
        row += (
            f",harness=codex,log_file={missing_log},"
            "parent_completion_delivery=codex-stop-hook,"
            f"parent_sid={session},failure_class=runtime,note=dead-runtime-exit\n"
        )
        jobs.write_text(row, encoding="utf-8")
        digest = hashlib.sha256(session.encode("utf-8")).hexdigest()
        state = jobs.parent / "parent-session-state" / f"{digest}.json"
        state.parent.mkdir(parents=True, exist_ok=True)
        state.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "parent_session_id_sha256": digest,
                    "attempt_ids": [attempt],
                    "delivered_attempt_ids": [attempt],
                }
            ),
            encoding="utf-8",
        )

        result = subprocess.run(
            [
                sys.executable,
                str(ROOT / "adapters/codex/bin/dispatch-harvest.py"),
                "--jobs",
                str(jobs),
                "--attempt-id",
                attempt,
                "--status",
                "all",
            ],
            text=True,
            capture_output=True,
            env=self.env(),
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("handoff_state=error", result.stdout)
        self.assertIn("parent_completion_receipt=consumed", result.stdout)
        self.assertFalse(state.exists())
        self.assertIn(
            "parent_completion_harvested=1",
            jobs.read_text(encoding="utf-8"),
        )

    def test_legacy_pending_receipt_is_recovered_only_after_exact_done_harvest(self):
        attempt = "att-native-stop-pending"
        session = "thread-native-stop-pending"
        jobs = self.base / "native-stop-pending.jobs.log"
        row = self.terminal_row(
            attempt, "PASS", "none", status="done", name="native-stop-pending.jsonl"
        )
        row = row.replace("dispatch_depth=2", "dispatch_depth=1").rstrip("\n")
        row += (
            ",parent_completion_delivery=codex-stop-hook,"
            f"parent_sid={session}\n"
        )
        jobs.write_text(row, encoding="utf-8")
        digest = hashlib.sha256(session.encode("utf-8")).hexdigest()
        state = jobs.parent / "parent-session-state" / f"{digest}.json"
        state.parent.mkdir(parents=True, exist_ok=True)
        state.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "parent_session_id_sha256": digest,
                    "attempt_ids": [attempt],
                    "delivered_attempt_ids": [],
                }
            ),
            encoding="utf-8",
        )

        result = subprocess.run(
            [
                sys.executable,
                str(ROOT / "adapters/codex/bin/dispatch-harvest.py"),
                "--jobs", str(jobs),
                "--attempt-id", attempt,
                "--status", "all",
            ],
            text=True,
            capture_output=True,
            env=self.env(),
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("parent_completion_receipt=consumed", result.stdout)
        self.assertFalse(state.exists())

        duplicate = subprocess.run(
            [
                sys.executable,
                str(ROOT / "adapters/codex/bin/dispatch-harvest.py"),
                "--jobs", str(jobs),
                "--attempt-id", attempt,
                "--status", "all",
            ],
            text=True,
            capture_output=True,
            env=self.env(),
        )
        self.assertNotEqual(duplicate.returncode, 0)
        self.assertIn("native-stop-receipt-not-delivered", duplicate.stdout)

    def test_ambiguous_or_legacy_selector_never_breadth_closes(self):
        for adapter in ("codex", "opencode"):
            with self.subTest(adapter=adapter, case="ambiguous"):
                jobs = self.base / f"{adapter}.ambiguous.log"
                jobs.write_text(
                    self.current_row("att-harvest-a", "same")
                    + self.current_row("att-harvest-b", "same"),
                    encoding="utf-8",
                )
                before = jobs.read_bytes()
                result = subprocess.run(
                    [
                        sys.executable,
                        str(ROOT / f"adapters/{adapter}/bin/dispatch-harvest.py"),
                        "--jobs", str(jobs), "--slug", "same", "--status", "open",
                        "--mark-done",
                    ],
                    text=True, capture_output=True, env=self.env(),
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("reason=ambiguous-selector", result.stdout)
                self.assertEqual(jobs.read_bytes(), before)
            with self.subTest(adapter=adapter, case="legacy"):
                jobs = self.base / f"{adapter}.legacy.log"
                jobs.write_text(
                    f"2026-07-21T00:00:00Z\topen\t/r\t/w\tlegacy\t"
                    "capability=code,depth=2,attempt_id=att-legacy\n",
                    encoding="utf-8",
                )
                before = jobs.read_bytes()
                result = subprocess.run(
                    [
                        sys.executable,
                        str(ROOT / f"adapters/{adapter}/bin/dispatch-harvest.py"),
                        "--jobs", str(jobs), "--slug", "legacy", "--status", "open",
                        "--mark-done",
                    ],
                    text=True, capture_output=True, env=self.env(),
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("legacy-attempt-row-read-only", result.stdout)
                self.assertEqual(jobs.read_bytes(), before)

    def test_codex_idempotent_mark_done_with_no_live_row_is_a_noop(self):
        jobs = self.base / "codex.done.log"
        jobs.write_text(
            self.current_row("att-harvest-done").replace(
                "\topen\t", "\tdone\t"
            ),
            encoding="utf-8",
        )
        result = subprocess.run(
            [
                sys.executable,
                str(ROOT / "adapters/codex/bin/dispatch-harvest.py"),
                "--jobs", str(jobs), "--slug", "worker", "--status", "open",
                "--mark-done",
            ],
            text=True, capture_output=True, env=self.env(),
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("marked_done=0", result.stdout)
        self.assertNotIn("UnboundLocalError", result.stderr)

    def test_codex_normalized_handoff_and_exact_closed_attempt_selector(self):
        artifact = self.artifact / "plans" / "final.md"
        artifact.parent.mkdir()
        artifact.write_text("ARTIFACT_BODY_SENTINEL")
        cases = (
            ("PASS", "none", "open", "none"),
            ("FAIL", "private-fail", "done", "worker-reported"),
            ("BLOCKED", "private-block", "done", "worker-reported"),
        )
        for verdict, blocker, status, blocker_reason in cases:
            with self.subTest(verdict=verdict):
                attempt = f"att-harvest-{verdict.lower()}"
                jobs = self.base / f"{verdict.lower()}.jobs.log"
                jobs.write_text(
                    self.terminal_row(
                        attempt, verdict, blocker, status=status, artifact=artifact
                    )
                )
                before = jobs.read_bytes()
                result = subprocess.run(
                    [sys.executable, str(ROOT / "adapters/codex/bin/dispatch-harvest.py"),
                     "--jobs", str(jobs), "--attempt-id", attempt, "--status", status],
                    text=True, capture_output=True, env=self.env(),
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("handoff_state=valid", result.stdout)
                self.assertIn(f"terminal_verdict={verdict}", result.stdout)
                self.assertIn("artifact_state=readable", result.stdout)
                self.assertIn(f"blocker_reason={blocker_reason}", result.stdout)
                self.assertNotIn("ARTIFACT_BODY_SENTINEL", result.stdout + result.stderr)
                if blocker != "none":
                    self.assertNotIn(blocker, result.stdout + result.stderr)
                self.assertEqual(jobs.read_bytes(), before, "read-only exact selector mutated row")

    def test_codex_harvest_reads_cross_harness_claude_result(self):
        attempt = "att-harvest-claude-pass"
        jobs = self.base / "claude-pass.jobs.log"
        jobs.write_text(self.claude_terminal_row(attempt, "PASS", "none"))
        before = jobs.read_bytes()
        result = subprocess.run(
            [sys.executable, str(ROOT / "adapters/codex/bin/dispatch-harvest.py"),
             "--jobs", str(jobs), "--attempt-id", attempt, "--status", "open"],
            text=True, capture_output=True, env=self.env(),
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("handoff_state=valid", result.stdout)
        self.assertIn("handoff_source=exact-claude-result", result.stdout)
        self.assertIn("terminal_verdict=PASS", result.stdout)
        self.assertEqual(jobs.read_bytes(), before)

    def test_codex_failure_detail_is_explicit_bounded_and_pass_is_rejected(self):
        blocker = "B\t" + "한" * 400
        diagnostic = "D\n\x01" + "界" * 400
        jobs = self.base / "detail.jobs.log"
        attempt = "att-harvest-detail"
        jobs.write_text(
            self.terminal_row(
                attempt, "FAIL", blocker, status="done", diagnostic=diagnostic,
                name="detail.codex.jsonl",
            )
        )
        base_args = [
            sys.executable, str(ROOT / "adapters/codex/bin/dispatch-harvest.py"),
            "--jobs", str(jobs), "--attempt-id", attempt, "--status", "done",
        ]
        default = subprocess.run(base_args, text=True, capture_output=True, env=self.env())
        self.assertEqual(default.returncode, 0, default.stdout + default.stderr)
        self.assertNotIn("_excerpt=", default.stdout)
        detailed = subprocess.run(
            base_args + ["--failure-detail"], text=True, capture_output=True, env=self.env()
        )
        self.assertEqual(detailed.returncode, 0, detailed.stdout + detailed.stderr)
        fields = dict(line.split("=", 1) for line in detailed.stdout.splitlines() if "=" in line)
        for key in ("blocker_detail_excerpt", "failure_diagnostic_excerpt"):
            self.assertLessEqual(len(fields[key].encode("utf-8")), 512)
            self.assertNotIn("\t", fields[key])
        self.assertEqual(fields["blocker_detail_truncated"], "1")
        self.assertEqual(fields["failure_diagnostic_truncated"], "1")

        pass_jobs = self.base / "pass-detail.jobs.log"
        pass_attempt = "att-harvest-pass-detail"
        pass_jobs.write_text(self.terminal_row(pass_attempt, "PASS", "none"))
        rejected = subprocess.run(
            [sys.executable, str(ROOT / "adapters/codex/bin/dispatch-harvest.py"),
             "--jobs", str(pass_jobs), "--attempt-id", pass_attempt, "--status", "open",
             "--failure-detail"],
            text=True, capture_output=True, env=self.env(),
        )
        self.assertEqual(rejected.returncode, 64)
        self.assertIn("reason=failure-detail-requires-terminal-failure", rejected.stdout)

    def test_done_registry_failures_without_handoffs_remain_inspectable(self):
        cases = (
            ("contract", "final-handoff-invalid", "final-handoff-invalid"),
            ("cancelled", "user-terminated", "user-terminated"),
            ("failed", "owner-launch-failed", "spawn-failed"),
        )
        for failure_class, note, reconcile_reason in cases:
            with self.subTest(failure_class=failure_class):
                attempt = f"att-registry-{failure_class}"
                jobs = self.base / f"registry-{failure_class}.jobs.log"
                row = self.current_row(attempt).replace("\topen\t", "\tdone\t").rstrip("\n")
                jobs.write_text(
                    row
                    + f",harness=codex,failure_class={failure_class},note={note},"
                    + f"reconcile_reason={reconcile_reason},launch_outcome=never-launched\n",
                    encoding="utf-8",
                )
                result = subprocess.run(
                    [
                        sys.executable,
                        str(ROOT / "adapters/codex/bin/dispatch-harvest.py"),
                        "--jobs", str(jobs), "--attempt-id", attempt,
                        "--status", "done", "--failure-detail",
                    ],
                    text=True,
                    capture_output=True,
                    env=self.env(),
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("failure_source=registry-terminal", result.stdout)
                self.assertIn(f"registry_failure_class={failure_class}", result.stdout)
                self.assertIn(f"registry_failure_note={note}", result.stdout)
                self.assertIn(
                    f"registry_reconcile_reason={reconcile_reason}", result.stdout
                )

    def test_codex_exact_log_binding_and_malformed_handoff(self):
        selected = "att-harvest-selected"
        foreign = "att-harvest-foreign"
        jobs = self.base / "binding.jobs.log"
        jobs.write_text(
            self.terminal_row(selected, "PASS", "none", slug="selected", name="selected.jsonl")
            + self.terminal_row(foreign, "FAIL", "foreign-private", slug="foreign", name="newer.jsonl")
        )
        result = subprocess.run(
            [sys.executable, str(ROOT / "adapters/codex/bin/dispatch-harvest.py"),
             "--jobs", str(jobs), "--attempt-id", selected, "--status", "open"],
            text=True, capture_output=True, env=self.env(),
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("terminal_verdict=PASS", result.stdout)
        self.assertNotIn("foreign-private", result.stdout + result.stderr)

        malformed_log = self.base / "malformed.codex.jsonl"
        malformed_log.write_text(json.dumps({"type": "item.completed", "item": {
            "type": "agent_message", "text": "RAW_MALFORMED_SENTINEL"}}) + "\n" +
            json.dumps({"type": "turn.completed"}) + "\n")
        malformed_jobs = self.base / "malformed.jobs.log"
        row = self.current_row("att-harvest-malformed").rstrip("\n")
        malformed_jobs.write_text(row +
            f",harness=codex,artifact_root={self.artifact},log_file={malformed_log}\n")
        malformed = subprocess.run(
            [sys.executable, str(ROOT / "adapters/codex/bin/dispatch-harvest.py"),
             "--jobs", str(malformed_jobs), "--attempt-id", "att-harvest-malformed",
             "--status", "open"],
            text=True, capture_output=True, env=self.env(),
        )
        self.assertEqual(malformed.returncode, 0)
        self.assertIn("handoff_state=invalid", malformed.stdout)
        self.assertNotIn("RAW_MALFORMED_SENTINEL", malformed.stdout + malformed.stderr)


if __name__ == "__main__":
    unittest.main()
