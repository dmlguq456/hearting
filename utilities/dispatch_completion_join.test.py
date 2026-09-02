#!/usr/bin/env python3

from __future__ import annotations

import base64
import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import unittest
from unittest import mock
import sys


HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location(
    "dispatch_completion_join", HERE / "dispatch_completion_join.py"
)
JOIN = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = JOIN
SPEC.loader.exec_module(JOIN)
sys.path.insert(0, str(HERE))
import dispatch_contract as D  # noqa: E402

# D-42 hermeticity: `classify_supervised_shell_command` and the supervisor
# state readers consult the LIVE session's dispatch environment on purpose, so
# running this suite from inside a supervised registered worker used to change
# its verdicts (`AGENT_DISPATCH_COMPLETION_MODE=supervised` alone turns the
# strict owner binding on and makes every unbound dispatch case classify as
# None). Each case supplies its own bindings as explicit arguments; drop the
# ambient ones so the suite measures the code, not its host session.
for _leaked in (
    "AGENT_DISPATCH_COMPLETION_MODE",
    "AGENT_DISPATCH_COMPLETION_STATE_FILE",
    "AGENT_DISPATCH_ATTEMPT_ID",
    "AGENT_DISPATCH_JOBS",
    "AGENT_DISPATCH_SELF_SLUG",
    "AGENT_ROUTE_FILE",
    "AGENT_ROUTE_ID",
    "AGENT_ROUTE_NODE",
):
    os.environ.pop(_leaked, None)


def row(
    status: str,
    attempt: str,
    parent: str,
    slug: str,
    sentinel: str = "",
    *,
    launch_outcome: str = "",
    process_metadata: dict[str, str] | None = None,
) -> str:
    meta = (
        "attempt_schema_version=2,dispatch_depth=2,transport=headless,"
        "execution_surface=registered-headless,registered_worker=1,"
        f"attempt_id={attempt},parent_attempt_id={parent},note={sentinel}"
    )
    if launch_outcome or (status == "done" and not process_metadata):
        meta += f",launch_outcome={launch_outcome or 'never-launched'}"
    for key, value in (process_metadata or {}).items():
        meta += f",{key}={value}"
    return f"2026-07-23T00:00:00Z\t{status}\t/repo\t/wt\t{slug}\t{meta}\n"


def session_row(
    status: str,
    attempt: str,
    parent_session: str,
    slug: str,
    *,
    native: bool = True,
    delivery: str | None = None,
    harness: str = "codex",
) -> str:
    meta = (
        "attempt_schema_version=2,dispatch_depth=1,transport=headless,"
        "execution_surface=registered-headless,registered_worker=1,"
        "launch_claimed=1,"
        f"attempt_id={attempt},parent_sid={parent_session},harness={harness}"
    )
    selected_delivery = delivery or ("codex-stop-hook" if native else "")
    if selected_delivery:
        meta += f",parent_completion_delivery={selected_delivery}"
    if status == "done":
        meta += ",launch_outcome=never-launched"
    return f"2026-07-23T00:00:00Z\t{status}\t/repo\t/wt\t{slug}\t{meta}\n"


def parallel_row(
    *,
    attempt: str,
    parent: str,
    node: str,
    group: str,
    declared_size: int,
    route_id: str,
    route_file: Path,
) -> str:
    return row(
        "open",
        attempt,
        parent,
        node,
        process_metadata={
            "worktree": str(route_file.parent),
            "route_file": str(route_file),
            "route_id": route_id,
            "route_node": node,
            "parallel_group": group,
            "replica_group": group,
            "reservation_kind": "parallel-batch",
            "batch_declared_size": str(declared_size),
            "batch_group": group,
            "batch_route_id": route_id,
            "batch_parent_attempt_id": parent,
            "batch_attempt_id": attempt,
            "batch_route_node": node,
        },
    )


class DispatchCompletionJoinTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.jobs = self.root / "jobs.log"
        self.live = self.root / "live.sh"
        self.live.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        self.live.chmod(0o755)

    def test_required_action_matches_receipt_and_harvest_contract(self):
        self.assertEqual(
            JOIN.required_action_for_attempt("open", {}), "complete-open"
        )
        self.assertEqual(
            JOIN.required_action_for_attempt("done", {"failure_class": "pass"}),
            "advance-completed",
        )
        self.assertEqual(
            JOIN.required_action_for_attempt("done", {"note": "completed-supervisor"}),
            "advance-completed",
        )
        self.assertEqual(
            JOIN.required_action_for_attempt("done", {"failure_class": "contract"}),
            "inspect-done-failure",
        )

    def marker_delivery_fixture(self, attempt: str = "att-delivery") -> str:
        evidence = self.root / "execute.md"
        evidence.write_text("fixture evidence\n", encoding="utf-8")
        route = self.root / "route.json"
        route_value = {
            "route_id": "rt-delivery",
            "route_hash": "sha256:" + "5" * 64,
            "registry_digest": "sha256:" + "6" * 64,
            "nodes": [{
                "id": "execute",
                "completion_gate": "code-execute",
                "dispatch_depth": 2,
            }],
        }
        route.write_text(json.dumps(route_value), encoding="utf-8")
        marker = self.root / "execute.json"
        marker_value = {
            "schema_version": 2,
            "sequence": 1,
            "route_id": route_value["route_id"],
            "route_hash": route_value["route_hash"],
            "registry_digest": route_value["registry_digest"],
            "node_id": "execute",
            "completion_gate": "code-execute",
            "attempt_id": attempt,
            "dispatch_depth": 2,
            "transport": "headless",
            "execution_surface": "registered-headless",
            "registered_worker": True,
            "fallback_hop": "same-harness-headless",
            "evidence": {
                "path": str(evidence),
                "sha256": hashlib.sha256(evidence.read_bytes()).hexdigest(),
            },
        }
        marker.write_text(json.dumps(marker_value), encoding="utf-8")
        (self.root / "execute.1.json").write_text(
            json.dumps(marker_value), encoding="utf-8"
        )
        (self.root / f"execute.{attempt}.attempt.json").write_text(
            json.dumps({
                "schema_version": 2,
                "route_id": route_value["route_id"],
                "node_id": "execute",
                "attempt_id": attempt,
                "dispatch_depth": 2,
                "transport": "headless",
                "execution_surface": "registered-headless",
                "registered_worker": True,
                "fallback_hop": "same-harness-headless",
                "evidence_sha256": marker_value["evidence"]["sha256"],
                "completion_marker": str(marker),
                "completion_marker_history": str(self.root / "execute.1.json"),
            }),
            encoding="utf-8",
        )
        metadata = (
            "attempt_schema_version=2,dispatch_depth=2,transport=headless,"
            "execution_surface=registered-headless,registered_worker=1,"
            f"fallback_hop=same-harness-headless,attempt_id={attempt},"
            "route_id=rt-delivery,route_hash=sha256:" + "5" * 64 + ","
            f"route_node=execute,route_file={route},completion_marker={marker},"
            "launch_outcome=never-launched"
        )
        raw = f"2026-08-25T00:00:00Z\topen\t/r\t/w\texecute\t{metadata}"
        self.jobs.write_text(raw + "\n", encoding="utf-8")
        return raw

    def test_current_delivery_state_advances_marker_open_row_once(self):
        self.marker_delivery_fixture()
        with mock.patch.object(
            JOIN,
            "attempt_process_quiescence",
            return_value=JOIN.ProcessQuiescence("quiescent", "fixture"),
        ) as process:
            state = JOIN.current_delivery_state(
                self.jobs, "att-delivery", parent_attempt_id="att-delivery"
            )
        self.assertEqual(process.call_count, 1)
        self.assertTrue(state.advanced)
        self.assertEqual((state.status, state.verdict), ("done", "PASS"))
        self.assertEqual(JOIN.delivery_classification(state), "success")
        self.assertEqual(state.owned_children, 0)

    def test_current_delivery_state_revision_race_is_attention_without_retry(self):
        self.marker_delivery_fixture("att-delivery-race")
        transaction = JOIN.marker_bound_delivery_transaction

        def race(*args, **kwargs):
            current = self.jobs.read_text(encoding="utf-8")
            self.jobs.write_text(
                current.replace(
                    "launch_outcome=never-launched",
                    "launch_outcome=never-launched,heartbeat=raced",
                ),
                encoding="utf-8",
            )
            return transaction(*args, **kwargs)

        with mock.patch.object(
            JOIN,
            "attempt_process_quiescence",
            return_value=JOIN.ProcessQuiescence("quiescent", "stale"),
        ), mock.patch.object(
            JOIN, "marker_bound_delivery_transaction", side_effect=race
        ) as called:
            state = JOIN.current_delivery_state(
                self.jobs,
                "att-delivery-race",
                parent_attempt_id="att-delivery-race",
            )
        self.assertEqual(called.call_count, 1)
        self.assertFalse(state.advanced)
        self.assertFalse(state.quiescent)
        self.assertEqual(state.status, "open")
        self.assertEqual(JOIN.delivery_classification(state), "attention")

    def test_delivery_timing_projection_is_complete_and_versioned(self):
        projected = JOIN.delivery_timing_fields(join_completed_ns=17)
        self.assertEqual(projected["delivery_timing_schema_version"], 1)
        self.assertEqual(projected["join_completed_ns"], 17)
        self.assertEqual(
            set(projected),
            {"delivery_timing_schema_version", *JOIN.DELIVERY_TIMING_POINTS},
        )

    def test_parallel_batch_waits_for_every_exact_child_and_ignores_foreign(self):
        parent = "att-parent"
        self.jobs.write_text(
            row("open", "att-a", parent, "a", "RAW_CHILD_SENTINEL")
            + row("open", "att-b", parent, "b")
            + row("open", "att-foreign", "att-other", "foreign"),
            encoding="utf-8",
        )

        def close_in_order() -> None:
            time.sleep(0.08)
            with self.jobs.open("a", encoding="utf-8") as handle:
                handle.write(row("done", "att-a", parent, "a"))
            time.sleep(0.14)
            with self.jobs.open("a", encoding="utf-8") as handle:
                handle.write(row("done", "att-b", parent, "b"))

        thread = threading.Thread(target=close_in_order)
        thread.start()
        started = time.monotonic()
        receipt = JOIN.join_batch(
            jobs=self.jobs,
            parent_attempt_id=parent,
            interval=0.02,
            timeout=2,
            liveness_command=[str(self.live)],
        )
        thread.join(timeout=2)
        elapsed = time.monotonic() - started
        self.assertGreaterEqual(elapsed, 0.18)
        self.assertEqual(receipt["state"], "ready")
        self.assertEqual(
            {child["attempt_id"] for child in receipt["children"]},
            {"att-a", "att-b"},
        )
        self.assertEqual(
            {child["required_action"] for child in receipt["children"]},
            {"inspect-done-failure"},
        )
        self.assertNotIn("RAW_CHILD_SENTINEL", json.dumps(receipt))

    def test_terminal_liveness_resumes_for_typed_harvest(self):
        terminal = self.root / "terminal.sh"
        terminal.write_text("#!/bin/sh\nexit 3\n", encoding="utf-8")
        terminal.chmod(0o755)
        self.jobs.write_text(
            row(
                "open",
                "att-a",
                "att-parent",
                "a",
                launch_outcome="reaped-before-publish",
            ),
            encoding="utf-8",
        )
        receipt = JOIN.join_batch(
            jobs=self.jobs,
            parent_attempt_id="att-parent",
            interval=0.02,
            timeout=1,
            liveness_command=[str(terminal)],
        )
        self.assertEqual(receipt["state"], "ready")
        self.assertEqual(receipt["children"][0]["reason"], "terminal-observed")
        self.assertEqual(receipt["children"][0]["required_action"], "complete-open")
        self.assertEqual(
            JOIN.pending_attempt_ids(
                JOIN.current_children(self.jobs, "att-parent")
            ),
            set(),
        )

    def test_running_registry_state_is_probed_as_open(self):
        probe = self.root / "probe.sh"
        probe.write_text(
            "#!/bin/sh\nawk -F '\\t' '$2 == \"open\" { found=1 } END { exit(found ? 3 : 9) }' \"$1\"\n",
            encoding="utf-8",
        )
        probe.chmod(0o755)
        self.jobs.write_text(
            row(
                "running",
                "att-a",
                "att-parent",
                "a",
                launch_outcome="reaped-before-publish",
            ),
            encoding="utf-8",
        )
        receipt = JOIN.join_batch(
            jobs=self.jobs,
            parent_attempt_id="att-parent",
            interval=0.02,
            timeout=1,
            liveness_command=[str(probe)],
        )
        self.assertEqual(receipt["state"], "ready")
        self.assertEqual(receipt["children"][0]["reason"], "terminal-observed")

    def test_done_row_waits_for_exact_process_to_exit(self):
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

        def stop_process() -> None:
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=5)

        self.addCleanup(stop_process)
        raw = Path(f"/proc/{proc.pid}/stat").read_text(encoding="utf-8")
        start = raw[raw.rfind(")") + 2 :].split()[19]
        process_metadata = {
            "pid": str(proc.pid),
            "pid_start": start,
            "pgid": str(proc.pid),
            "pid_observer_ns": os.readlink("/proc/self/ns/pid"),
        }
        self.jobs.write_text(
            row(
                "done",
                "att-a",
                "att-parent",
                "a",
                process_metadata=process_metadata,
            ),
            encoding="utf-8",
        )
        draining = JOIN.join_batch(
            jobs=self.jobs,
            parent_attempt_id="att-parent",
            interval=0.02,
            timeout=0.08,
            liveness_command=[str(self.live)],
        )
        self.assertEqual(draining["state"], "timeout")
        self.assertEqual(draining["children"][0]["reason"], "process-alive")
        proc.terminate()
        proc.wait(timeout=5)
        ready = JOIN.join_batch(
            jobs=self.jobs,
            parent_attempt_id="att-parent",
            interval=0.02,
            timeout=1,
            liveness_command=[str(self.live)],
        )
        self.assertEqual(ready["state"], "ready")

    def test_done_namespace_local_row_polls_until_post_exit_receipt_is_complete(self):
        attempt = "att-namespace-receipt"
        parent = "att-parent"
        metadata = {
            "pid": "999999",
            "pid_start": "42",
            "pgid": "999999",
            "pid_scope": "namespace-local",
            "pid_ns": os.readlink("/proc/self/ns/pid"),
            "pid_observer_ns": os.readlink("/proc/self/ns/pid"),
        }
        self.jobs.write_text(
            row("done", attempt, parent, "a", process_metadata=metadata),
            encoding="utf-8",
        )

        def publish_receipt() -> None:
            time.sleep(0.12)
            complete = dict(
                metadata,
                launch_lifecycle="foreground-scoped",
                launch_outcome="governed-process-reaped",
                group_reap_proof="pgid-empty-v1",
                group_reap_pgid="999999",
            )
            with self.jobs.open("a", encoding="utf-8") as handle:
                handle.write(row("done", attempt, parent, "a", process_metadata=complete))

        thread = threading.Thread(target=publish_receipt)
        thread.start()
        started = time.monotonic()
        receipt = JOIN.join_batch(
            jobs=self.jobs,
            parent_attempt_id=parent,
            interval=0.02,
            timeout=1,
            liveness_command=[str(self.live)],
        )
        thread.join(timeout=1)
        self.assertGreaterEqual(time.monotonic() - started, 0.1)
        self.assertEqual(receipt["state"], "ready")
        self.assertEqual(receipt["children"][0]["reason"], "registry-closed")

    def test_open_namespace_terminal_envelope_cannot_bypass_receipt_gate(self):
        attempt = "att-open-namespace-receipt"
        parent = "att-parent"
        log = self.root / "worker.codex.jsonl"
        log.write_text(
            "\n".join(json.dumps(event) for event in [
                {
                    "type": "item.completed",
                    "item": {
                        "type": "agent_message",
                        "text": "artifact: -\nverdict: PASS\nblocker: none",
                    },
                },
                {"type": "turn.completed"},
            ]) + "\n",
            encoding="utf-8",
        )
        namespace = os.readlink("/proc/self/ns/pid")
        metadata = {
            "pid": "999999",
            "pid_start": "42",
            "pgid": "999999",
            "pid_scope": "namespace-local",
            "pid_ns": namespace,
            "pid_observer_ns": namespace,
            "log_file": str(log),
        }
        self.jobs.write_text(
            row("open", attempt, parent, "a", process_metadata=metadata),
            encoding="utf-8",
        )
        blocked = JOIN.join_batch(
            jobs=self.jobs,
            parent_attempt_id=parent,
            interval=0.02,
            timeout=0.08,
            liveness_command=[str(self.live)],
        )
        self.assertEqual(blocked["state"], "timeout")
        self.assertEqual(blocked["children"][0]["reason"], "process-unverifiable")

        complete = dict(
            metadata,
            launch_lifecycle="foreground-scoped",
            launch_outcome="governed-process-reaped",
            group_reap_proof="pgid-empty-v1",
            group_reap_pgid="999999",
        )
        with self.jobs.open("a", encoding="utf-8") as handle:
            handle.write(row("open", attempt, parent, "a", process_metadata=complete))
        ready = JOIN.join_batch(
            jobs=self.jobs,
            parent_attempt_id=parent,
            interval=0.02,
            timeout=0.5,
            liveness_command=[str(self.live)],
        )
        self.assertEqual(ready["state"], "ready")
        self.assertEqual(ready["children"][0]["reason"], "terminal-observed")

    def test_open_namespace_terminal_probe_cannot_bypass_receipt_gate(self):
        terminal = self.root / "terminal.sh"
        terminal.write_text("#!/bin/sh\nexit 3\n", encoding="utf-8")
        terminal.chmod(0o755)
        namespace = os.readlink("/proc/self/ns/pid")
        metadata = {
            "pid": "999999",
            "pid_start": "42",
            "pgid": "999999",
            "pid_scope": "namespace-local",
            "pid_ns": namespace,
            "pid_observer_ns": namespace,
        }
        self.jobs.write_text(
            row(
                "open",
                "att-open-namespace-probe",
                "att-parent",
                "a",
                process_metadata=metadata,
            ),
            encoding="utf-8",
        )
        receipt = JOIN.join_batch(
            jobs=self.jobs,
            parent_attempt_id="att-parent",
            interval=0.02,
            timeout=0.08,
            liveness_command=[str(terminal)],
        )
        self.assertEqual(receipt["state"], "timeout")
        self.assertEqual(
            receipt["children"][0]["reason"], "process-unverifiable"
        )

    def test_done_namespace_local_row_with_partial_receipt_stays_pending(self):
        metadata = {
            "pid": "999999",
            "pid_start": "42",
            "pgid": "999999",
            "pid_scope": "namespace-local",
            "pid_ns": os.readlink("/proc/self/ns/pid"),
            "pid_observer_ns": os.readlink("/proc/self/ns/pid"),
            "launch_outcome": "governed-process-reaped",
        }
        self.jobs.write_text(
            row(
                "done",
                "att-partial-receipt",
                "att-parent",
                "a",
                process_metadata=metadata,
            ),
            encoding="utf-8",
        )
        receipt = JOIN.join_batch(
            jobs=self.jobs,
            parent_attempt_id="att-parent",
            interval=0.02,
            timeout=0.08,
            liveness_command=[str(self.live)],
        )
        self.assertEqual(receipt["state"], "timeout")
        self.assertEqual(receipt["children"][0]["reason"], "process-unverifiable")

    def test_timeout_is_one_bounded_receipt(self):
        self.jobs.write_text(
            row("open", "att-a", "att-parent", "a", "RAW_TIMEOUT_SENTINEL"),
            encoding="utf-8",
        )
        receipt = JOIN.join_batch(
            jobs=self.jobs,
            parent_attempt_id="att-parent",
            interval=0.02,
            timeout=0.08,
            liveness_command=[str(self.live)],
        )
        self.assertEqual(receipt["state"], "timeout")
        self.assertEqual(receipt["children"][0]["readiness"], "pending")
        self.assertNotIn("RAW_TIMEOUT_SENTINEL", json.dumps(receipt))

    def test_unexpected_join_failure_is_still_one_typed_receipt(self):
        # Regression for the 2026-08-14 candidate 2 supervisor-join death: a
        # non-JoinContractError escaped main(), so the supervisor read an empty
        # stdout and could only report `join-receipt-json-invalid`.
        self.jobs.write_text(row("open", "att-a", "att-parent", "a"), encoding="utf-8")
        with mock.patch.object(
            JOIN, "join_batch", side_effect=RuntimeError("boom-raw-detail")
        ):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = JOIN.main(
                    ["--jobs", str(self.jobs), "--parent-attempt-id", "att-parent"]
                )
        self.assertEqual(code, 69)
        receipt = json.loads(out.getvalue())
        self.assertEqual(receipt["state"], "contract-error")
        self.assertEqual(receipt["parent_attempt_id"], "att-parent")
        self.assertEqual(receipt["reason"], "join-internal-error-RuntimeError")
        self.assertEqual(receipt["children"], [])
        self.assertNotIn("boom-raw-detail", out.getvalue())

    def test_unexpected_join_failure_exits_typed_as_a_subprocess(self):
        # The supervisor consumes this over a pipe: stdout must be exactly one
        # JSON receipt and the exit code must stay inside the join protocol.
        self.jobs.write_text(row("open", "att-a", "att-parent", "a"), encoding="utf-8")
        harness = (
            "import importlib.util,sys\n"
            "spec=importlib.util.spec_from_file_location('j',%r)\n"
            "m=importlib.util.module_from_spec(spec)\n"
            "sys.modules['j']=m\n"
            "spec.loader.exec_module(m)\n"
            "def boom(**kwargs):\n"
            "    raise ValueError('boom-raw-detail')\n"
            "m.join_batch=boom\n"
            "raise SystemExit(m.main(sys.argv[1:]))\n"
            % str(HERE / "dispatch_completion_join.py")
        )
        result = subprocess.run(
            [sys.executable, "-c", harness,
             "--jobs", str(self.jobs), "--parent-attempt-id", "att-parent"],
            text=True, capture_output=True, check=False,
        )
        self.assertEqual(result.returncode, 69, result.stderr)
        receipt = json.loads(result.stdout)
        self.assertEqual(receipt["reason"], "join-internal-error-ValueError")
        self.assertNotIn("boom-raw-detail", result.stdout)

    def test_expected_attempt_set_fails_closed(self):
        self.jobs.write_text(row("done", "att-a", "att-parent", "a"), encoding="utf-8")
        with self.assertRaises(JOIN.JoinContractError):
            JOIN.join_batch(
                jobs=self.jobs,
                parent_attempt_id="att-parent",
                expected_attempts={"att-a", "att-missing"},
                liveness_command=[str(self.live)],
            )

    def test_supervisor_phase_state_is_atomic_bounded_and_parent_scoped(self):
        state = self.root / "runtime" / "parent.json"
        JOIN.write_supervisor_state(state, "att-parent", {"att-b", "att-a"})
        self.assertEqual(
            JOIN.read_supervisor_state(state, "att-parent"),
            {"att-a", "att-b"},
        )
        self.assertEqual(state.stat().st_mode & 0o777, 0o600)
        self.assertIsNone(JOIN.read_supervisor_state(state, "att-foreign"))
        state.write_text('{"schema_version":1,"parent_attempt_id":"att-parent"}', encoding="utf-8")
        self.assertIsNone(JOIN.read_supervisor_state(state, "att-parent"))
        JOIN.remove_supervisor_state(state)
        self.assertFalse(state.exists())

    def test_supervisor_outbox_is_idempotent_and_detects_row_advance(self):
        state_path = self.root / "runtime" / "parent.json"
        child = JOIN.ChildRow(
            0,
            "open",
            "child",
            "att-a",
            row("open", "att-a", "att-parent", "child").rstrip("\n"),
            {
                "attempt_schema_version": "2",
                "attempt_id": "att-a",
                "parent_attempt_id": "att-parent",
            },
        )
        receipt = {
            "schema_version": 2,
            "state": "ready",
            "parent_attempt_id": "att-parent",
            "children": [
                {
                    "attempt_id": "att-a",
                    "status": "open",
                    "required_action": "complete-open",
                }
            ],
        }
        first = JOIN.prepare_supervisor_outbox(
            state_path, "att-parent", set(), receipt, [child]
        )
        second = JOIN.prepare_supervisor_outbox(
            state_path, "att-parent", set(), receipt, [child]
        )
        self.assertEqual(first.outbox, second.outbox)
        loaded = JOIN.read_supervisor_phase_state(state_path, "att-parent")
        self.assertEqual(loaded, first)
        self.assertEqual(JOIN.supervisor_outbox_row_state(first, [child]), "current")
        advanced = JOIN.ChildRow(
            child.order,
            "done",
            child.slug,
            child.attempt_id,
            child.raw.replace("\topen\t", "\tdone\t"),
            child.metadata,
        )
        self.assertEqual(
            JOIN.supervisor_outbox_row_state(first, [advanced]), "row-advanced"
        )
        self.assertTrue(
            JOIN.consume_supervisor_outbox_attempts(
                state_path, "att-parent", {"att-a"}
            )
        )
        consumed = JOIN.read_supervisor_phase_state(state_path, "att-parent")
        self.assertIsNotNone(consumed)
        self.assertIsNone(consumed.outbox)
        self.assertEqual(consumed.phase, "running-turn")
        self.assertFalse(
            JOIN.consume_supervisor_outbox_attempts(
                state_path, "att-parent", {"att-a"}
            )
        )
        transitions = [
            json.loads(line)
            for line in state_path.with_name(
                state_path.name + ".transitions.jsonl"
            ).read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(
            [(event["previous_phase"], event["phase"]) for event in transitions],
            [("absent", "deliverable"), ("deliverable", "deliverable"),
             ("deliverable", "running-turn")],
        )
        self.assertTrue(all(event["outer_pid"] == os.getpid() for event in transitions))
        self.assertTrue(all(event["outer_pid_start"] for event in transitions))

    def test_supervisor_outbox_partial_consume_preserves_same_receipt(self):
        state_path = self.root / "runtime" / "parent-batch.json"
        children = [
            JOIN.ChildRow(
                index,
                "done",
                f"child-{index}",
                attempt,
                row("done", attempt, "att-parent", f"child-{index}").rstrip("\n"),
                {
                    "attempt_schema_version": "2",
                    "attempt_id": attempt,
                    "parent_attempt_id": "att-parent",
                    "failure_class": "fail",
                },
            )
            for index, attempt in enumerate(("att-a", "att-b"))
        ]
        receipt = {
            "schema_version": 2,
            "state": "ready",
            "parent_attempt_id": "att-parent",
            "children": [
                {
                    "attempt_id": child.attempt_id,
                    "status": "done",
                    "required_action": "inspect-done-failure",
                }
                for child in children
            ],
        }
        prepared = JOIN.prepare_supervisor_outbox(
            state_path, "att-parent", set(), receipt, children
        )
        self.assertTrue(
            JOIN.consume_supervisor_outbox_attempts(
                state_path, "att-parent", {"att-a"}
            )
        )
        partial = JOIN.read_supervisor_phase_state(state_path, "att-parent")
        self.assertEqual(partial.outbox.receipt_id, prepared.outbox.receipt_id)
        self.assertEqual(partial.outbox.receipt_digest, prepared.outbox.receipt_digest)
        self.assertEqual(partial.outbox.receipt, receipt)
        self.assertEqual(partial.outbox.consumed_attempt_ids, frozenset({"att-a"}))
        self.assertFalse(
            JOIN.consume_supervisor_outbox_attempts(
                state_path, "att-parent", {"att-a"}
            )
        )
        self.assertTrue(
            JOIN.consume_supervisor_outbox_attempts(
                state_path, "att-parent", {"att-b"}
            )
        )

    def test_supervisor_outbox_refresh_preserves_identity_and_advances_action(self):
        state_path = self.root / "runtime" / "parent-refresh.json"
        child = JOIN.ChildRow(
            0,
            "open",
            "child",
            "att-a",
            row("open", "att-a", "att-parent", "child").rstrip("\n"),
            {
                "attempt_schema_version": "2",
                "attempt_id": "att-a",
                "parent_attempt_id": "att-parent",
            },
        )
        receipt = {
            "schema_version": 2,
            "state": "ready",
            "parent_attempt_id": "att-parent",
            "children": [{
                "attempt_id": "att-a",
                "status": "open",
                "required_action": "complete-open",
            }],
        }
        prepared = JOIN.prepare_supervisor_outbox(
            state_path, "att-parent", set(), receipt, [child]
        )
        advanced = JOIN.ChildRow(
            child.order,
            "done",
            child.slug,
            child.attempt_id,
            child.raw.replace("\topen\t", "\tdone\t") + ",failure_class=fail",
            {**child.metadata, "failure_class": "fail"},
        )
        self.jobs.write_text(advanced.raw + "\n", encoding="utf-8")
        refreshed = JOIN.refresh_supervisor_outbox_actions(
            state_path, "att-parent", [advanced], jobs=self.jobs
        )
        self.assertEqual(refreshed.outbox.receipt_id, prepared.outbox.receipt_id)
        self.assertNotEqual(
            refreshed.outbox.receipt_digest, prepared.outbox.receipt_digest
        )
        self.assertNotEqual(
            refreshed.outbox.row_revisions, prepared.outbox.row_revisions
        )
        child_receipt = refreshed.outbox.receipt["children"][0]
        self.assertEqual(child_receipt["status"], "done")
        self.assertEqual(
            child_receipt["required_action"], "inspect-done-failure"
        )
        self.assertEqual(child_receipt["reason"], "terminal-failure-or-unclosed")

    def test_delivery_timing_rejects_out_of_order_boundaries_and_stamps_once(self):
        timing = JOIN.delivery_timing_fields(last_child_terminal_ns=10)
        timing = JOIN.advance_delivery_timing(timing, "join_completed_ns", at_ns=20)
        timing = JOIN.advance_delivery_timing(timing, "same_thread_resume_ns", at_ns=30)
        self.assertEqual(
            JOIN.advance_delivery_timing(
                timing, "same_thread_resume_ns", at_ns=999
            )["same_thread_resume_ns"],
            30,
        )
        with self.assertRaisesRegex(JOIN.JoinContractError, "delivery-timing-order-invalid"):
            JOIN.advance_delivery_timing(timing, "exact_harvest_ns", at_ns=25)

    def test_session_children_select_only_stamped_exact_direct_rows(self):
        session = "thread-exact"
        depth_two = row("open", "att-depth-two", "att-parent", "depth-two")
        depth_two = depth_two.replace(
            "parent_attempt_id=att-parent", f"parent_sid={session}"
        )
        self.jobs.write_text(
            session_row("open", "att-native", session, "native")
            + session_row("open", "att-foreign", "thread-foreign", "foreign")
            + session_row("open", "att-legacy", session, "legacy", native=False)
            + depth_two,
            encoding="utf-8",
        )
        rows = JOIN.current_session_children(self.jobs, session)
        self.assertEqual([item.attempt_id for item in rows], ["att-native"])

    def test_session_batch_waits_for_every_exact_child(self):
        session = "thread-parent"
        self.jobs.write_text(
            session_row("open", "att-a", session, "a")
            + session_row("open", "att-b", session, "b")
            + session_row("open", "att-foreign", "thread-other", "foreign"),
            encoding="utf-8",
        )

        def close_batch() -> None:
            time.sleep(0.06)
            with self.jobs.open("a", encoding="utf-8") as handle:
                handle.write(session_row("done", "att-a", session, "a"))
            time.sleep(0.06)
            with self.jobs.open("a", encoding="utf-8") as handle:
                handle.write(session_row("done", "att-b", session, "b"))

        thread = threading.Thread(target=close_batch)
        thread.start()
        receipt = JOIN.join_session_batch(
            jobs=self.jobs,
            parent_session_id=session,
            interval=0.02,
            timeout=1,
            liveness_command=[str(self.live)],
        )
        thread.join(timeout=1)
        self.assertEqual(receipt["state"], "ready")
        self.assertEqual(receipt["parent_session_id"], session)
        self.assertEqual(
            {child["attempt_id"] for child in receipt["children"]},
            {"att-a", "att-b"},
        )

    def test_managed_session_batch_selects_exact_cross_harness_attempts(self):
        session = "thread-managed"
        managed = JOIN.MANAGED_SESSION_PARENT_DELIVERY
        self.jobs.write_text(
            session_row(
                "done", "att-codex", session, "codex-child",
                delivery=managed, harness="codex",
            )
            + session_row(
                "done", "att-claude", session, "claude-child",
                delivery=managed, harness="claude",
            )
            + session_row(
                "done", "att-legacy", session, "legacy-child",
                delivery=JOIN.SESSION_PARENT_DELIVERY,
            )
            + session_row(
                "done", "att-foreign", "thread-other", "foreign-child",
                delivery=managed,
            ),
            encoding="utf-8",
        )
        receipt = JOIN.join_session_batch(
            jobs=self.jobs,
            parent_session_id=session,
            expected_attempts={"att-codex", "att-claude"},
            parent_completion_delivery=managed,
            interval=0.02,
            timeout=1,
            liveness_command=[str(self.live)],
        )
        self.assertEqual(receipt["state"], "ready")
        self.assertEqual(
            {child["attempt_id"] for child in receipt["children"]},
            {"att-codex", "att-claude"},
        )
        self.assertNotIn("att-legacy", json.dumps(receipt))
        self.assertNotIn("att-foreign", json.dumps(receipt))

    def test_parent_session_state_is_atomic_bounded_and_hashed(self):
        session = "thread-private"
        state = JOIN.parent_session_state_path(self.jobs, session)
        self.assertNotIn(session, state.name)
        JOIN.register_parent_session_attempt(state, session, "att-a")
        JOIN.register_parent_session_attempt(state, session, "att-b")
        pending = JOIN.read_parent_session_batch_state(state, session)
        self.assertIsNotNone(pending)
        self.assertEqual(set(pending.attempt_ids), {"att-a", "att-b"})
        self.assertEqual(set(pending.delivered_attempt_ids), set())
        JOIN.write_parent_session_state(
            state,
            session,
            {"att-b", "att-a"},
            attempt_ids={"att-b", "att-a"},
        )
        self.assertEqual(
            JOIN.read_parent_session_state(state, session),
            {"att-a", "att-b"},
        )
        self.assertEqual(state.stat().st_mode & 0o777, 0o600)
        self.assertIsNone(JOIN.read_parent_session_state(state, "thread-foreign"))
        with self.assertRaises(JOIN.JoinContractError):
            JOIN.consume_parent_session_attempt(
                state,
                session,
                "att-a",
                before_consume=lambda: False,
            )
        self.assertEqual(
            JOIN.read_parent_session_state(state, session),
            {"att-a", "att-b"},
        )
        self.assertTrue(JOIN.consume_parent_session_attempt(state, session, "att-a"))
        self.assertEqual(JOIN.read_parent_session_state(state, session), {"att-b"})
        self.assertTrue(JOIN.consume_parent_session_attempt(state, session, "att-b"))
        self.assertFalse(state.exists())

    def test_supervised_command_classifier_admits_only_exact_phase_actions(self):
        open_attempts = {"att-a", "att-b"}
        harvest = JOIN.classify_supervised_shell_command(
            base=JOIN.ROOT,
            command=(
                "adapters/codex/bin/preflight.sh harvest "
                "--attempt-id att-a --status open"
            ),
            open_attempt_ids=open_attempts,
            parent_slug="owner",
        )
        self.assertEqual(
            harvest,
            JOIN.SupervisorShellAction("harvest", "att-a", status="open"),
        )
        absolute_harvest = JOIN.classify_supervised_shell_command(
            base=JOIN.ROOT,
            command=(
                f"{JOIN.ROOT / 'adapters/codex/bin/preflight.sh'} harvest "
                "--attempt-id att-b --status open --mark-done"
            ),
            open_attempt_ids=open_attempts,
            parent_slug="owner",
        )
        self.assertEqual(
            absolute_harvest,
            JOIN.SupervisorShellAction(
                "harvest", "att-b", status="open", mark_done=True
            ),
        )
        harvest_all = JOIN.classify_supervised_shell_command(
            base=JOIN.ROOT,
            command=(
                "adapters/codex/bin/preflight.sh harvest "
                "--attempt-id att-a --status all"
            ),
            open_attempt_ids=open_attempts,
            parent_slug="owner",
        )
        self.assertEqual(
            harvest_all,
            JOIN.SupervisorShellAction("harvest", "att-a", status="all"),
        )
        dispatch = JOIN.classify_supervised_shell_command(
            base=JOIN.ROOT,
            command=(
                "python3 utilities/dispatch-node.py --route /tmp/route.json "
                "--node implement --adapter claude --action start --slug worker-b "
                "--parent owner -- --jobs /tmp/jobs.log"
            ),
            open_attempt_ids=open_attempts,
            parent_slug="owner",
        )
        self.assertEqual(dispatch, JOIN.SupervisorShellAction("dispatch"))
        batch = JOIN.classify_supervised_shell_command(
            base=JOIN.ROOT,
            command=(
                "adapters/codex/bin/preflight.sh dispatch-batch "
                "--route /tmp/route.json --replica-group plan "
                "--action start --slug-prefix review --parent owner "
                "--jobs /tmp/jobs.log"
            ),
            open_attempt_ids=open_attempts,
            parent_slug="owner",
        )
        self.assertEqual(batch, JOIN.SupervisorShellAction("dispatch-batch"))
        for command in (
            "adapters/codex/bin/preflight.sh harvest --attempt-id att-c --status open",
            "adapters/codex/bin/preflight.sh harvest --attempt-id att-a --status open; git status",
            (
                "utilities/dispatch-node.py --route /tmp/route.json --node implement "
                "--adapter codex --action start --slug worker-b --parent foreign"
            ),
            (
                "/tmp/python3 utilities/dispatch-node.py --route /tmp/route.json "
                "--node implement --adapter codex --action start --slug worker-b "
                "--parent owner"
            ),
            "git status --short",
            (
                "adapters/codex/bin/preflight.sh dispatch-batch "
                "--route /tmp/route.json --replica-group plan --action start "
                "--slug-prefix review --parent foreign"
            ),
            (
                "adapters/codex/bin/preflight.sh dispatch-batch "
                "--route /tmp/route.json --replica-group plan --action start "
                "--slug-prefix review --parent owner --jobs /tmp/a.log "
                "--jobs /tmp/b.log"
            ),
        ):
            with self.subTest(command=command):
                self.assertIsNone(
                    JOIN.classify_supervised_shell_command(
                        base=JOIN.ROOT,
                        command=command,
                        open_attempt_ids=open_attempts,
                        parent_slug="owner",
                    )
                )

    def test_supervised_env_alone_turns_on_the_strict_owner_binding(self):
        # Production behaviour the hermeticity block above exists for: the live
        # session's completion mode is a real input, so any suite that lets it
        # leak in measures its host instead of the code.
        self.assertFalse(
            JOIN._strict_supervisor_binding_requested(
                jobs=None, parent_attempt_id="", route_file=None, route_id=""
            )
        )
        with mock.patch.dict(
            JOIN.os.environ, {"AGENT_DISPATCH_COMPLETION_MODE": "supervised"}
        ):
            self.assertTrue(
                JOIN._strict_supervisor_binding_requested(
                    jobs=None, parent_attempt_id="", route_file=None, route_id=""
                )
            )
            self.assertIsNone(
                JOIN.classify_supervised_shell_command(
                    base=JOIN.ROOT,
                    command=(
                        "python3 utilities/dispatch-node.py "
                        "--route /tmp/route.json --node implement "
                        "--adapter claude --action start --slug worker-b "
                        "--parent owner -- --jobs /tmp/jobs.log"
                    ),
                    open_attempt_ids={"att-a"},
                    parent_slug="owner",
                )
            )

    def test_strict_supervisor_admits_only_one_missing_leg_of_three_way_group(self):
        parent = "att-parent"
        route_id = "route-n3"
        group = "frame"
        route_file = self.root / "route.json"
        route_file.write_text(
            json.dumps(
                {
                    "route_id": route_id,
                    "nodes": [
                        {
                            "id": node,
                            "dispatch_depth": 2,
                            "parallel_group": group,
                            "replica_group": group,
                        }
                        for node in ("frame-a", "frame-b", "frame-c")
                    ],
                }
            ),
            encoding="utf-8",
        )

        def command() -> str:
            return (
                "adapters/codex/bin/preflight.sh dispatch-batch "
                f"--route {route_file} --parallel-group {group} --action start "
                f"--slug-prefix frame --parent owner --jobs {self.jobs}"
            )

        rows = [
            parallel_row(
                attempt=f"att-{suffix}",
                parent=parent,
                node=f"frame-{suffix}",
                group=group,
                declared_size=3,
                route_id=route_id,
                route_file=route_file,
            )
            for suffix in ("a", "b", "c")
        ]

        self.jobs.write_text(rows[0], encoding="utf-8")
        self.assertIsNone(
            JOIN.classify_supervised_shell_command(
                base=JOIN.ROOT,
                command=command(),
                open_attempt_ids={"att-a"},
                parent_slug="owner",
                jobs=self.jobs,
                parent_attempt_id=parent,
                route_file=route_file,
                route_id=route_id,
            )
        )

        self.jobs.write_text("".join(rows[:2]), encoding="utf-8")
        self.assertEqual(
            JOIN.classify_supervised_shell_command(
                base=JOIN.ROOT,
                command=command(),
                open_attempt_ids={"att-a", "att-b"},
                parent_slug="owner",
                jobs=self.jobs,
                parent_attempt_id=parent,
                route_file=route_file,
                route_id=route_id,
            ),
            JOIN.SupervisorShellAction("dispatch-batch"),
        )

        self.jobs.write_text("".join(rows), encoding="utf-8")
        self.assertIsNone(
            JOIN.classify_supervised_shell_command(
                base=JOIN.ROOT,
                command=command(),
                open_attempt_ids={"att-a", "att-b", "att-c"},
                parent_slug="owner",
                jobs=self.jobs,
                parent_attempt_id=parent,
                route_file=route_file,
                route_id=route_id,
            )
        )

    def test_strict_supervisor_rejects_direct_dispatch_of_parallel_leg(self):
        parent = "att-parent"
        route_id = "route-n2"
        group = "plan"
        route_file = self.root / "route.json"
        route_file.write_text(
            json.dumps(
                {
                    "route_id": route_id,
                    "nodes": [
                        {
                            "id": node,
                            "dispatch_depth": 2,
                            "parallel_group": group,
                            "replica_group": group,
                        }
                        for node in ("plan-a", "plan-b")
                    ],
                }
            ),
            encoding="utf-8",
        )
        self.jobs.write_text(
            parallel_row(
                attempt="att-a",
                parent=parent,
                node="plan-a",
                group=group,
                declared_size=2,
                route_id=route_id,
                route_file=route_file,
            ),
            encoding="utf-8",
        )
        command = (
            f"python3 utilities/dispatch-node.py --route {route_file} "
            "--node plan-b --adapter claude --action start --slug plan-b "
            f"--parent owner -- --jobs {self.jobs}"
        )
        self.assertIsNone(
            JOIN.classify_supervised_shell_command(
                base=JOIN.ROOT,
                command=command,
                open_attempt_ids={"att-a"},
                parent_slug="owner",
                jobs=self.jobs,
                parent_attempt_id=parent,
                route_file=route_file,
                route_id=route_id,
            )
        )


def sealed_cancellation_metadata() -> dict[str, str]:
    # A dead-but-locally-scannable identity: this is the wedge's real shape --
    # a supervisor/operator observer that CAN see the process is gone (same
    # namespace, pid genuinely absent) but was blocked from trusting that
    # local read without a durable receipt. pid_observer_ns matches this
    # process's own namespace so authoritative_process_identities finds a
    # local candidate; pid=99999996 is a pid that does not exist.
    observer = os.readlink("/proc/self/ns/pid")
    return {
        "pid_scope": "namespace-local",
        "pid": "99999996", "pid_start": "1", "pgid": "99999996",
        "pid_observer_ns": observer, "pid_ns": observer,
        "cancellation_quiescence_receipt": D.ATTEMPT_CANCELLATION_QUIESCENCE_RECEIPT,
        "cancellation_receipt_digest": "sha256:" + "c" * 64,
        "quiescence_pgid_proof": D.GROUP_REAP_PROOF,
        "quiescence_descendant_proof": D.ATTEMPT_DESCENDANT_PROOF,
    }


def sealed_cancellation_metadata_without_receipt() -> dict[str, str]:
    observer = os.readlink("/proc/self/ns/pid")
    return {
        "pid_scope": "namespace-local",
        "pid": "99999996", "pid_start": "1", "pgid": "99999996",
        "pid_observer_ns": observer, "pid_ns": observer,
    }


def load_supervisor(name: str):
    spec = importlib.util.spec_from_file_location(
        f"harvest_parity_{name}", HERE / f"{name}.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class HarvestVocabularyTest(unittest.TestCase):
    """DC-3, DC-4 (plan SS5.7 / SS3.4 D1, D1b): one harvest vocabulary."""

    def setUp(self):
        self.claude = load_supervisor("claude-session-supervisor")
        self.codex = load_supervisor("codex-app-server-supervisor")
        self.jobs = "/fixture/jobs.log"
        self.base = {"attempt_id": "att-a"}
        self.open_ids = {"att-a"}

    def _classify(self, line: str):
        # base is the worker cwd in production (registered-parent-park.py
        # derives it the same way); codex's completion_prompt emits a
        # ROOT-relative path, so base must actually be ROOT for the relative
        # form to resolve to the real contract path.
        return JOIN.classify_supervised_shell_command(
            base=JOIN.ROOT,
            command=line,
            open_attempt_ids=self.open_ids,
            parent_slug="fixture-slug",
            jobs=Path(self.jobs),
        )

    def test_incident_command_with_jobs_now_classifies(self):
        # D-1: the exact command string the incident denied.
        line = (
            f"{self.claude.SHARED_HARVEST_SURFACE} harvest --jobs {self.jobs} "
            "--attempt-id att-a --status done --failure-detail"
        )
        action = self._classify(line)
        self.assertIsNotNone(action)
        self.assertEqual(action.kind, "harvest")
        self.assertEqual(action.attempt_id, "att-a")
        self.assertEqual(action.status, "done")
        self.assertTrue(action.failure_detail)

    def test_open_status_with_jobs_and_mark_done_classifies(self):
        # D-2
        line = (
            f"{self.claude.SHARED_HARVEST_SURFACE} harvest --jobs {self.jobs} "
            "--attempt-id att-a --status open --mark-done"
        )
        action = self._classify(line)
        self.assertIsNotNone(action)
        self.assertTrue(action.mark_done)

    def test_jobs_supplied_twice_or_valueless_is_rejected(self):
        # D-3: the value-option arity check still rejects malformed --jobs.
        twice = (
            f"{self.claude.SHARED_HARVEST_SURFACE} harvest --jobs {self.jobs} "
            f"--jobs {self.jobs} --attempt-id att-a --status open --mark-done"
        )
        self.assertIsNone(self._classify(twice))
        valueless = (
            f"{self.claude.SHARED_HARVEST_SURFACE} harvest --jobs "
            "--attempt-id att-a --status open --mark-done"
        )
        self.assertIsNone(self._classify(valueless))

    def test_jobs_pointed_at_another_registry_is_rejected(self):
        line = (
            f"{self.claude.SHARED_HARVEST_SURFACE} harvest --jobs /other/jobs.log "
            "--attempt-id att-a --status open --mark-done"
        )
        self.assertIsNone(self._classify(line))

    def test_precheck_on_satisfiable_receipt_is_the_only_in_tree_outcome(self):
        # D-6a: with D1 applied, the precheck must never itself find an
        # unsatisfiable line for any producer/required_action combination.
        for required_action in ("complete-open", "inspect-done-failure", "advance-completed"):
            receipt = {"children": [{"attempt_id": "att-a", "required_action": required_action}]}
            for prompt in (
                self.claude.completion_prompt(receipt, jobs=self.jobs),
                self.codex.completion_prompt(receipt, jobs=self.jobs),
            ):
                lines = JOIN.harvest_command_lines(prompt)
                satisfiable, reason = JOIN.supervisor_receipt_satisfiable(
                    lines,
                    base=JOIN.ROOT,
                    open_attempt_ids=self.open_ids,
                    parent_slug="fixture-slug",
                    jobs=Path(self.jobs),
                )
                self.assertTrue(satisfiable, (required_action, lines, reason))

    def test_guarded_attempt_ids_is_the_hooks_own_derivation(self):
        # D-6a (extension): the precheck's guarded set is the park hook's set.
        # open|running, plus non-quiescent rows, plus the outbox's unconsumed
        # attempts -- one function, so a supervisor cannot answer satisfiable
        # for a command the guard then denies.
        rows = [
            # `done` with no terminal evidence is still non-quiescent, so it
            # stays guarded: process liveness alone cannot release the guard at
            # the closure boundary.
            JOIN.ChildRow(0, "done", "child-a", "att-a", "", {}),
            JOIN.ChildRow(1, "open", "child-b", "att-b", "", {}),
        ]
        self.assertEqual(
            JOIN.supervisor_guarded_attempt_ids(rows, None), {"att-a", "att-b"}
        )
        outbox = JOIN.SupervisorOutbox(
            "receipt-1", "digest-1", frozenset({"att-c", "att-d"}), (),
            None, frozenset({"att-d"}),
        )
        # att-c is an unconsumed outbox attempt and joins the set; att-d was
        # consumed and does not.
        self.assertEqual(
            JOIN.supervisor_guarded_attempt_ids(rows, outbox),
            {"att-a", "att-b", "att-c"},
        )

    def test_producer_guard_parity_no_hand_written_literals(self):
        # D-4: the durable guard against vocabulary drift. Every line either
        # producer can emit, for every required_action value, must classify.
        receipts = [
            {"children": [{"attempt_id": "att-a", "required_action": action}]}
            for action in ("complete-open", "inspect-done-failure", "advance-completed")
        ]
        prompts = []
        for receipt in receipts:
            prompts.append(self.claude.completion_prompt(receipt, jobs=self.jobs))
            prompts.append(self.codex.completion_prompt(receipt, jobs=self.jobs))
        prompts.append(self.claude.remediation_prompt({"att-a"}, jobs=self.jobs))
        prompts.append(self.codex.remediation_prompt({"att-a"}))
        checked_any = False
        for prompt in prompts:
            for line in JOIN.harvest_command_lines(prompt):
                checked_any = True
                self.assertIsNotNone(
                    self._classify(line), f"guard rejected producer line: {line!r}"
                )
        self.assertTrue(checked_any, "no harvest lines were generated to check")


class OwnerReleaseJoinTest(unittest.TestCase):
    """J-1..J-3, J-6, J-7 (plan SS5.4): the cancellation receipt clears the wedge."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.jobs = self.root / "jobs.log"

    def test_open_namespace_local_row_with_no_receipt_stays_pending(self):
        # J-1 (regression: the wedge as it exists today). Same locally-dead
        # identity as J-2/J-3 but with no cancellation receipt and a status
        # that forces the terminal-receipt gate (done), so this is the exact
        # wedge the guard clause exists to leave closed absent a receipt.
        self.jobs.write_text(
            row("done", "att-j1", "att-parent", "j1",
                process_metadata=sealed_cancellation_metadata_without_receipt()),
            encoding="utf-8",
        )
        self.assertIn(
            "att-j1",
            JOIN.pending_attempt_ids(JOIN.current_children(self.jobs, "att-parent")),
        )

    def test_automatic_seal_and_close_clears_readiness(self):
        # J-2
        self.jobs.write_text(
            row("done", "att-j2", "att-parent", "j2",
                process_metadata={
                    **sealed_cancellation_metadata(),
                    "failure_class": "cancelled",
                    "note": "cancelled-receipt-unavailable",
                }),
            encoding="utf-8",
        )
        self.assertNotIn(
            "att-j2",
            JOIN.pending_attempt_ids(JOIN.current_children(self.jobs, "att-parent")),
        )

    def test_manually_cancelled_and_sealed_row_clears_readiness(self):
        # J-3 (closes incident item 4)
        self.jobs.write_text(
            row("done", "att-j3", "att-parent", "j3",
                process_metadata={
                    **sealed_cancellation_metadata(),
                    "failure_class": "cancelled",
                    "note": "cancelled-receipt-unavailable",
                    "classifier_source": "operator-receiptless-cancel-v1",
                }),
            encoding="utf-8",
        )
        self.assertNotIn(
            "att-j3",
            JOIN.pending_attempt_ids(JOIN.current_children(self.jobs, "att-parent")),
        )

    def test_sd79_negative_cancelled_row_is_not_a_general_successor_pass(self):
        # J-6 (SD-79 negative, end-to-end)
        self.jobs.write_text(
            row("done", "att-j6", "att-parent", "j6",
                process_metadata={
                    **sealed_cancellation_metadata(),
                    "failure_class": "cancelled",
                    "note": "cancelled-receipt-unavailable",
                }),
            encoding="utf-8",
        )
        rows = JOIN.current_children(self.jobs, "att-parent")
        self.assertNotIn("att-j6", JOIN.pending_attempt_ids(rows))
        meta = rows[0].metadata
        self.assertEqual(D.post_exit_receipt_reason(meta), "")
        self.assertEqual(D.cancellation_receipt_reason(meta), "cancellation-receipt-sealed")

    def test_genuine_detached_drain_receipt_stays_ready(self):
        # J-7 (regression: the healthy path -- a real portable post-exit
        # receipt, not the new cancellation receipt)
        self.jobs.write_text(
            row("done", "att-j7", "att-parent", "j7",
                process_metadata={
                    "pid_scope": "namespace-local",
                    "pid": "41", "pid_start": "900", "pgid": "41",
                    "pid_observer_ns": "pid:[401]", "pid_ns": "pid:[401]",
                    "launch_lifecycle": "detached",
                    "launch_outcome": "governed-process-group-drained",
                    "group_reap_proof": D.GROUP_REAP_PROOF,
                    "group_reap_pgid": "41",
                    "attempt_descendant_proof": D.ATTEMPT_DESCENDANT_PROOF,
                    "attempt_descendant_observer_ns": "pid:[401]",
                }),
            encoding="utf-8",
        )
        self.assertNotIn(
            "att-j7",
            JOIN.pending_attempt_ids(JOIN.current_children(self.jobs, "att-parent")),
        )


class FinishedChildClosure(unittest.TestCase):
    """The supervised owner has no legal way to close a route-bound child.

    `dispatch-harvest --mark-done` refuses a route-bound node with
    `route-completion-required`, and the park hook admits only that harvest
    command — so the supervisor must close it from evidence itself or the batch
    deadlocks (observed 5x on 2026-07-28).
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        # artifact-root.sh resolves the canonical root from the worktree, so the
        # fixture must present a real one rather than an arbitrary directory.
        self.artifact = self.base / ".agent_reports"
        self.artifact.mkdir()
        self.environment = mock.patch.dict(
            os.environ,
            {
                "AGENT_ARTIFACT_ROOT": str(self.artifact),
                "AGENT_MODEL_GOVERNOR_ROOT": str(
                    self.artifact / ".runtime" / "model-worker-governor"
                ),
            },
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.jobs = self.base / "jobs.log"
        self.jobs.touch()

    def child(self, *, route: bool = True, verdict: str = "PASS",
              artifact: str | None = "brief.md", quiescent: bool = False,
              attempt_id: str = "att-child") -> JOIN.ChildRow:
        log = self.base / f"{attempt_id}.claude.jsonl"
        target = "-" if artifact is None else str(self.artifact / artifact)
        if artifact is not None:
            (self.artifact / artifact).write_text("evidence\n", encoding="utf-8")
        log.write_text(
            json.dumps({"type": "system", "subtype": "init"}) + "\n"
            + json.dumps({
                "type": "result", "subtype": "success", "is_error": False,
                "result": f"artifact: {target}\nverdict: {verdict}\nblocker: none",
            }) + "\n",
            encoding="utf-8",
        )
        meta = {
            "attempt_id": attempt_id, "attempt_schema_version": "2", "dispatch_depth": "2",
            "transport": "headless", "execution_surface": "registered-headless",
            "registered_worker": "1", "fallback_hop": "same-harness-headless",
            "harness": "claude", "log_file": str(log),
            "artifact_root": str(self.artifact),
        }
        if quiescent:
            # No PID recorded and an atomic-launch outcome that proves no
            # governed process remains -> attempt_process_quiescence()
            # classifies this as quiescent without needing a real PID probe.
            meta["launch_outcome"] = "reaped-before-publish"
        if route:
            meta["route_file"] = str(self.base / "route.json")
            meta["route_node"] = "frame"
        raw = "\t".join([
            "2026-07-28T06:00:00.000000Z", "open", str(self.base), str(self.base),
            f"{attempt_id}-slug", ",".join(f"{k}={v}" for k, v in meta.items()),
        ])
        return JOIN.ChildRow(
            order=0, status="open", slug=f"{attempt_id}-slug", attempt_id=attempt_id,
            raw=raw, metadata=meta,
        )

    def test_route_bound_child_is_closed_through_the_completion_path(self):
        calls: list[list[str]] = []

        def fake_completion(command):
            calls.append(command)
            return ""

        real = JOIN.run_route_completion
        JOIN.run_route_completion = fake_completion
        try:
            reason = JOIN.close_finished_child(self.child(), jobs=self.jobs)
        finally:
            JOIN.run_route_completion = real
        self.assertEqual(reason, "")
        self.assertEqual(len(calls), 1)
        command = calls[0]
        self.assertIn("complete", command)
        self.assertIn("capability-route.py", " ".join(command))
        # exact attempt metadata, not a bare mark-done
        for flag in ("--attempt-id", "--node", "--evidence", "--jobs",
                     "--execution-surface", "--fallback-hop"):
            self.assertIn(flag, command)
        self.assertNotIn("--mark-done", command)

    def test_absent_terminal_evidence_never_invents_completion(self):
        row_without_log = self.child()
        row_without_log.metadata["log_file"] = str(self.base / "missing.jsonl")
        called = []
        real = JOIN.run_route_completion
        JOIN.run_route_completion = lambda command: called.append(command) or ""
        try:
            reason = JOIN.close_finished_child(row_without_log, jobs=self.jobs)
        finally:
            JOIN.run_route_completion = real
        self.assertTrue(reason.startswith("terminal-"), reason)
        self.assertEqual(called, [])

    def test_child_without_an_artifact_stays_open(self):
        reason = JOIN.close_finished_child(self.child(artifact=None), jobs=self.jobs)
        self.assertTrue(reason.startswith("evidence-"), reason)

    def test_non_route_bound_child_is_left_to_the_harvest_path(self):
        reason = JOIN.close_finished_child(self.child(route=False), jobs=self.jobs)
        self.assertEqual(reason, "not-route-bound")

    def test_rejected_completion_keeps_the_child_unresolved(self):
        real = JOIN.run_route_completion
        JOIN.run_route_completion = lambda command: "completion-rejected"
        try:
            outcomes = JOIN.reconcile_finished_children(
                {"att-child": self.child()}, {"att-child"}, jobs=self.jobs
            )
        finally:
            JOIN.run_route_completion = real
        self.assertEqual(outcomes, {"att-child": "completion-rejected"})

    def test_wrapper_reaped_pass_failure_closes_typed_instead_of_ghosting(self):
        child = self.child(quiescent=True)
        self.jobs.write_text(child.raw + "\n", encoding="utf-8")
        calls = []
        real = JOIN.run_route_completion
        JOIN.run_route_completion = lambda command: calls.append(command) or "completion-rejected"
        try:
            reason = JOIN.close_wrapper_pass(child, jobs=self.jobs)
        finally:
            JOIN.run_route_completion = real
        self.assertEqual(reason, "completion-rejected")
        self.assertEqual(len(calls), 2)
        text = self.jobs.read_text(encoding="utf-8")
        self.assertIn("\tdone\t", text)
        self.assertIn("note=dead-route-completion-rejected", text)
        self.assertIn("failure_class=contract", text)
        self.assertNotIn("completed-marker", text)

    # -- Phase 3 (plan.md, round_1 finding 1): typed BLOCKED/FAIL closure ----

    def test_quiescent_blocked_row_with_readable_artifact_closes_typed_never_completes(self):
        # round_1 finding 1: a readable artifact must NOT reach route
        # completion when the verdict is BLOCKED — this is the exact ordering
        # bug the arbiter flagged (readable branch was unguarded).
        row = self.child(verdict="BLOCKED", quiescent=True)
        self.jobs.write_text(row.raw + "\n", encoding="utf-8")
        calls = []
        real = JOIN.run_route_completion
        JOIN.run_route_completion = lambda command: calls.append(command) or ""
        try:
            reason = JOIN.close_finished_child(row, jobs=self.jobs)
        finally:
            JOIN.run_route_completion = real
        self.assertEqual(reason, "")
        self.assertEqual(calls, [], "route completion must never run for a BLOCKED verdict")
        lines = self.jobs.read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertIn("done", lines[0])
        self.assertIn("dead-worker-blocked", lines[0])
        self.assertIn("completion-join-terminal-verdict-v1", lines[0])
        self.assertFalse(
            (self.base / ".dispatch" / "completion").exists(),
            "a typed BLOCKED closure must never create a completion marker",
        )

    def test_quiescent_fail_row_with_readable_artifact_closes_typed_never_completes(self):
        row = self.child(verdict="FAIL", quiescent=True)
        self.jobs.write_text(row.raw + "\n", encoding="utf-8")
        calls = []
        real = JOIN.run_route_completion
        JOIN.run_route_completion = lambda command: calls.append(command) or ""
        try:
            reason = JOIN.close_finished_child(row, jobs=self.jobs)
        finally:
            JOIN.run_route_completion = real
        self.assertEqual(reason, "")
        self.assertEqual(calls, [], "route completion must never run for a FAIL verdict")
        lines = self.jobs.read_text(encoding="utf-8").strip().splitlines()
        self.assertIn("dead-worker-fail", lines[0])
        self.assertIn("completion-join-terminal-verdict-v1", lines[0])

    def test_review_worker_fail_with_artifact_closes_completed_review_blocking(self):
        # OPERATIONS §5.10 owner-closure extension: the join books a reviewer's
        # blocking-findings FAIL as a finished review, seals the artifact it
        # named on the row, and still never runs route completion for it.
        row = self.child(verdict="FAIL", artifact="round_1.md", quiescent=True)
        row.metadata["worker_type"] = "review"
        self.jobs.write_text(row.raw + "\n", encoding="utf-8")
        calls = []
        real = JOIN.run_route_completion
        JOIN.run_route_completion = lambda command: calls.append(command) or ""
        try:
            reason = JOIN.close_finished_child(row, jobs=self.jobs)
        finally:
            JOIN.run_route_completion = real
        self.assertEqual(reason, "")
        self.assertEqual(calls, [], "route completion must never run for a FAIL verdict")
        line = self.jobs.read_text(encoding="utf-8").strip().splitlines()[0]
        self.assertIn("note=completed-review-blocking", line)
        self.assertNotIn("dead-worker-fail", line)
        self.assertIn("reconcile_reason=typed-review-blocking", line)
        self.assertIn("classifier_source=completion-join-terminal-verdict-v1", line)
        self.assertIn("review_artifact_b64=", line)
        # B47-3: this classifier source stays unlabeled on the verdict axis
        self.assertNotIn("failure_class=", line)

    def test_review_worker_fail_without_artifact_stays_a_dead_worker(self):
        row = self.child(verdict="FAIL", artifact=None, quiescent=True)
        row.metadata["worker_type"] = "review"
        self.jobs.write_text(row.raw + "\n", encoding="utf-8")
        reason = JOIN.close_finished_child(row, jobs=self.jobs)
        self.assertEqual(reason, "")
        line = self.jobs.read_text(encoding="utf-8").strip().splitlines()[0]
        self.assertIn("dead-worker-fail", line)
        self.assertNotIn("completed-review-blocking", line)

    def test_blocked_row_without_readable_artifact_also_closes_typed(self):
        # The pre-existing branch (no readable artifact) must keep closing
        # BLOCKED/FAIL typed too, not just the new readable-artifact branch.
        row = self.child(verdict="BLOCKED", artifact=None, quiescent=True)
        self.jobs.write_text(row.raw + "\n", encoding="utf-8")
        reason = JOIN.close_finished_child(row, jobs=self.jobs)
        self.assertEqual(reason, "")
        lines = self.jobs.read_text(encoding="utf-8").strip().splitlines()
        self.assertIn("dead-worker-blocked", lines[0])

    def test_b47_3_invalid_envelope_sets_class_and_ledger_delta_one(self):
        # §4 (2)-A: `_close_invalid_envelope_child()` binds `failure_class`
        # on `classifier_source`, not unconditionally -- only the default
        # `completion-join-invalid-envelope-v1` path (a malformed handoff)
        # gets the label, and it also writes exactly one W5 degradation row.
        row = self.child(quiescent=True)
        row.metadata["route_id"] = "rt-b47-3"
        row.metadata["route_hash"] = "sha256:b47-3"
        log = Path(row.metadata["log_file"])
        log.write_text(
            json.dumps({"type": "system", "subtype": "init"}) + "\n"
            + json.dumps({
                "type": "result", "subtype": "error", "is_error": True,
                "result": "boom",
            }) + "\n",
            encoding="utf-8",
        )
        self.jobs.write_text(row.raw + "\n", encoding="utf-8")
        reason = JOIN.close_finished_child(row, jobs=self.jobs)
        self.assertEqual(reason, "")
        text = self.jobs.read_text(encoding="utf-8")
        self.assertIn("dead-invalid-envelope", text)
        self.assertIn("failure_class=invalid-envelope", text)
        ledger_path = self.base / "degradations" / "rt-b47-3.jsonl"
        self.assertTrue(ledger_path.exists())
        lines = ledger_path.read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(lines), 1, "delta must be exactly one ledger row")
        ledger_row = json.loads(lines[0])
        self.assertEqual(ledger_row["writer"], "dispatch_completion_join.py")
        self.assertEqual(ledger_row["reason"], "invalid-envelope")
        self.assertEqual(ledger_row["route_id"], "rt-b47-3")
        self.assertEqual(ledger_row["route_hash"], "sha256:b47-3")

    def test_b47_3_dead_missing_result_never_gets_the_invalid_envelope_class(self):
        # A-34: a distinct classifier_source (missing-result) must not carry
        # the `invalid-envelope` failure_class or write a ledger row -- only
        # the default classifier does (Appendix A regression guard).
        row = self.child(quiescent=True)
        row.metadata["route_id"] = "rt-b47-3-missing"
        row.metadata["log_file"] = ""
        self.jobs.write_text(row.raw + "\n", encoding="utf-8")
        reason = JOIN.close_finished_child(row, jobs=self.jobs)
        self.assertEqual(reason, "")
        text = self.jobs.read_text(encoding="utf-8")
        self.assertIn("dead-missing-result", text)
        self.assertNotIn("failure_class=invalid-envelope", text)
        ledger_path = self.base / "degradations" / "rt-b47-3-missing.jsonl"
        self.assertFalse(ledger_path.exists())

    def test_live_process_blocked_row_stays_open(self):
        # Quiescence precondition must not be relaxed: a still-draining
        # worker (no terminal-envelope-implied quiescence and no exited
        # process evidence) is never closed early, BLOCKED or not.
        row = self.child(verdict="BLOCKED", quiescent=False)
        self.jobs.write_text(row.raw + "\n", encoding="utf-8")
        before = self.jobs.read_text(encoding="utf-8")
        reason = JOIN.close_finished_child(row, jobs=self.jobs)
        self.assertNotEqual(reason, "")
        self.assertEqual(self.jobs.read_text(encoding="utf-8"), before)

    def test_second_reconcile_pass_over_closed_row_is_a_no_op(self):
        row = self.child(verdict="BLOCKED", quiescent=True)
        self.jobs.write_text(row.raw + "\n", encoding="utf-8")
        first = JOIN.close_finished_child(row, jobs=self.jobs)
        self.assertEqual(first, "")
        before = self.jobs.read_text(encoding="utf-8")
        outcomes = JOIN.reconcile_finished_children(
            {"att-child": row}, {"att-child"}, jobs=self.jobs
        )
        after = self.jobs.read_text(encoding="utf-8")
        self.assertEqual(before, after, "a second pass must not mutate an already-closed row")
        self.assertIn("att-child", outcomes)

    def test_sibling_rows_stay_byte_identical_no_breadth_close(self):
        sibling = self.child(verdict="PASS", attempt_id="att-sibling", quiescent=False)
        target = self.child(verdict="BLOCKED", quiescent=True)
        self.jobs.write_text(sibling.raw + "\n" + target.raw + "\n", encoding="utf-8")
        sibling_line_before = sibling.raw
        reason = JOIN.close_finished_child(target, jobs=self.jobs)
        self.assertEqual(reason, "")
        after_lines = self.jobs.read_text(encoding="utf-8").splitlines()
        self.assertIn(sibling_line_before, after_lines, "the sibling row must be untouched (SD-77)")

    def test_marker_wins_existing_completion_not_overwritten_by_envelope_note(self):
        # SD-72: if a route/node already carries an exact completion marker,
        # a typed envelope note must never overwrite that success. This is
        # exercised at the PASS+readable path, which is unchanged by Phase 3
        # and still reaches route completion (a real marker write is the
        # completion command's own responsibility, verified untouched here).
        calls = []
        real = JOIN.run_route_completion
        JOIN.run_route_completion = lambda command: calls.append(command) or ""
        try:
            reason = JOIN.close_finished_child(self.child(verdict="PASS"), jobs=self.jobs)
        finally:
            JOIN.run_route_completion = real
        self.assertEqual(reason, "")
        self.assertEqual(len(calls), 1)
        self.assertIn("complete", calls[0])

    def test_pass_verdict_behaviour_unchanged(self):
        reason = JOIN.close_finished_child(self.child(artifact=None), jobs=self.jobs)
        self.assertTrue(reason.startswith("evidence-"), reason)

    def test_codex_turn_completed_blocked_envelope_closes_typed_same_as_claude(self):
        # SD-72/SD-77 require both adapter terminal forms (round_1 graft):
        # Codex's `item.completed` + `turn.completed` pair must classify and
        # close identically to the Claude `result` fixture above.
        log = self.base / "att-codex-child.codex.jsonl"
        artifact_path = self.artifact / "brief.md"
        artifact_path.write_text("evidence\n", encoding="utf-8")
        log.write_text(
            "\n".join(json.dumps(row) for row in [
                {"type": "system", "subtype": "init"},
                {
                    "type": "item.completed",
                    "item": {
                        "type": "agent_message",
                        "text": f"artifact: {artifact_path}\nverdict: BLOCKED\nblocker: stuck",
                    },
                },
                {"type": "turn.completed"},
            ]) + "\n",
            encoding="utf-8",
        )
        meta = {
            "attempt_id": "att-codex-child", "attempt_schema_version": "2",
            "dispatch_depth": "2", "transport": "headless",
            "execution_surface": "registered-headless", "registered_worker": "1",
            "fallback_hop": "same-harness-headless", "harness": "codex",
            "log_file": str(log), "artifact_root": str(self.artifact),
            "route_file": str(self.base / "route.json"), "route_node": "frame",
            "launch_outcome": "reaped-before-publish",
        }
        raw = "\t".join([
            "2026-07-28T06:00:00.000000Z", "open", str(self.base), str(self.base),
            "att-codex-child-slug", ",".join(f"{k}={v}" for k, v in meta.items()),
        ])
        row = JOIN.ChildRow(
            order=0, status="open", slug="att-codex-child-slug",
            attempt_id="att-codex-child", raw=raw, metadata=meta,
        )
        self.jobs.write_text(raw + "\n", encoding="utf-8")
        calls = []
        real = JOIN.run_route_completion
        JOIN.run_route_completion = lambda command: calls.append(command) or ""
        try:
            reason = JOIN.close_finished_child(row, jobs=self.jobs)
        finally:
            JOIN.run_route_completion = real
        self.assertEqual(reason, "")
        self.assertEqual(calls, [])
        lines = self.jobs.read_text(encoding="utf-8").strip().splitlines()
        self.assertIn("dead-worker-blocked", lines[0])


class StageAdvanceReceiptNegotiationTest(unittest.TestCase):
    """SD-110 plan.md §6 A-18 (receipt v1/v2 byte-identity for un-negotiated
    consumers, proved with golden-byte comparison rather than mere successful
    decode) and A-19 (every refusal reason still delivers a receipt that is
    byte-identical to pre-SD-110, regardless of whether the consumer has
    negotiated the v3 schema)."""

    def _record(self, *, outcome, reason=""):
        advanced = outcome == "advanced"
        return {
            "schema_version": 1,
            "stage_advance_id": "sadv-" + "0" * 64,
            "route_id": "rt-0000000000000000",
            "route_hash": "sha256:" + "0" * 64,
            "predecessor_node": "plan",
            "predecessor_terminal_attempt_id": "att-plan",
            "successor_node": "execute" if advanced else None,
            "successor_attempt_id": "att-execute" if advanced else None,
            "claim_key": (
                ["sha256:" + "0" * 64, "execute", 0] if advanced else None
            ),
            "brief_template_digest": ("sha256:" + "1" * 64) if advanced else "",
            "outcome": outcome,
            "reason": reason,
            "registered": advanced,
            "started": advanced,
            "child_spawned": advanced,
        }

    def test_stage_advance_receipt_block_is_the_flat_record_shape(self):
        record = self._record(outcome="advanced")
        block = JOIN.stage_advance_receipt_block(record)
        self.assertEqual(set(block), set(JOIN.STAGE_ADVANCE_RECEIPT_FIELDS))
        expected = {
            field: record[field] for field in JOIN.STAGE_ADVANCE_RECEIPT_FIELDS
        }
        self.assertEqual(
            json.dumps(block, sort_keys=True), json.dumps(expected, sort_keys=True)
        )

    def test_a18_recordless_or_refused_receipt_is_byte_identical(self):
        receipt = {
            "schema_version": 2,
            "state": "ready",
            "parent_attempt_id": "att-parent",
            "children": [],
        }
        golden = json.dumps(receipt, sort_keys=True)
        # no stage-advance attempt this delivery.
        out = JOIN.receipt_with_stage_advance(receipt, stage_advance_record=None)
        self.assertIs(out, receipt)
        self.assertEqual(json.dumps(out, sort_keys=True), golden)
        # a refused record is equally inert -- no separate "negotiated" flag
        # can make a refused outcome carry the v3 block.
        out = JOIN.receipt_with_stage_advance(
            receipt, stage_advance_record=self._record(outcome="refused")
        )
        self.assertIs(out, receipt)
        self.assertEqual(json.dumps(out, sort_keys=True), golden)
        # SD-119: the chain-advance path exists but a join with no
        # sub-session chain metadata is a no-op -- this receipt never carries
        # a chain key, byte-identical to pre-SD-119.
        import dispatch_subsession_advance as subsession_advance
        from types import SimpleNamespace

        no_chain = subsession_advance.coordinate_chain_advance_from_joined_rows(
            Path("/nonexistent/jobs.registry"), "att-parent", {
                "att-child": SimpleNamespace(
                    attempt_id="att-child", status="done", metadata={},
                )
            },
        )
        self.assertIsNone(no_chain)
        self.assertNotIn("chain_id", golden)

    def test_no_negotiated_kwarg_exists(self):
        """T1 correction: `receipt_with_stage_advance` used to accept an
        independent `negotiated` bool that could disagree with the record's
        own `outcome` (13.32.1-(3)B's forbidden state -- advanced outcome,
        un-negotiated delivery). Asserting the parameter is entirely gone
        (not merely defaulted) is what makes that combination
        unrepresentable rather than "not currently produced": a future call
        site cannot resurrect the second knob by accident."""

        import inspect  # noqa: PLC0415

        params = inspect.signature(JOIN.receipt_with_stage_advance).parameters
        self.assertNotIn("negotiated", params)

    def test_a19_every_refusal_reason_is_byte_identical(self):
        import dispatch_stage_advance as SA  # noqa: PLC0415

        receipt = {
            "schema_version": 2,
            "state": "ready",
            "parent_attempt_id": "att-parent",
            "children": [],
        }
        golden = json.dumps(receipt, sort_keys=True)
        self.assertEqual(len(SA.REFUSAL_REASONS), 16)
        for reason in SA.REFUSAL_REASONS:
            with self.subTest(reason=reason):
                out = JOIN.receipt_with_stage_advance(
                    receipt,
                    stage_advance_record=self._record(
                        outcome="refused", reason=reason
                    ),
                )
                self.assertIs(out, receipt)
                self.assertEqual(json.dumps(out, sort_keys=True), golden)

    def test_advanced_outcome_attaches_v3_block(self):
        receipt = {
            "schema_version": 2,
            "state": "ready",
            "parent_attempt_id": "att-parent",
            "children": [],
        }
        record = self._record(outcome="advanced")
        out = JOIN.receipt_with_stage_advance(receipt, stage_advance_record=record)
        self.assertIsNot(out, receipt)
        self.assertEqual(out["schema_version"], JOIN.STAGE_ADVANCE_SCHEMA_VERSION)
        self.assertEqual(out["state"], receipt["state"])
        self.assertEqual(out["parent_attempt_id"], receipt["parent_attempt_id"])
        self.assertEqual(
            out[JOIN.STAGE_ADVANCE_RECEIPT_KEY],
            JOIN.stage_advance_receipt_block(record),
        )

    def test_typed_stage_advance_block_strict_decode(self):
        record = self._record(outcome="advanced")
        block = JOIN.stage_advance_receipt_block(record)
        self.assertEqual(JOIN.typed_stage_advance_block(block), block)
        with self.assertRaises(JOIN.JoinContractError):
            JOIN.typed_stage_advance_block({**block, "outcome": "bogus"})
        with self.assertRaises(JOIN.JoinContractError):
            JOIN.typed_stage_advance_block({**block, "registered": "true"})
        with self.assertRaises(JOIN.JoinContractError):
            JOIN.typed_stage_advance_block(
                {k: v for k, v in block.items() if k != "reason"}
            )
        with self.assertRaises(JOIN.JoinContractError):
            JOIN.typed_stage_advance_block({**block, "schema_version": 2})
        with self.assertRaises(JOIN.JoinContractError):
            JOIN.typed_stage_advance_block("not-a-dict")


class CanonicalReceiptAndSealTest(unittest.TestCase):
    """SD-111 P0: canonical digest selection and receipt-body sealing (D-2/C-2)."""

    def _receipt(self, **overrides):
        base = {
            "schema_version": 2,
            "state": "delivered",
            "parent_attempt_id": "att-0000000000000000000000000000aaaa",
            "job_registry": "/tmp/sd111p0/jobs.log",
            "children": [
                {
                    "attempt_id": "att-0000000000000000000000000000bbbb",
                    "status": "done",
                    "readiness": "ready",
                    "reason": "terminal-failure-or-unclosed",
                    "required_action": "inspect-done-failure",
                    "harness": "claude",
                    "delivery_classification": "attention",
                }
            ],
            "delivery_classification": "attention",
        }
        base.update(overrides)
        return base

    def test_a_two_carrier_context_renders_are_byte_identical(self):
        # No render actually depends on process-local state once
        # delivery_timing is excluded; two independent computations over the
        # same logical receipt must agree exactly (probe 2 reproduction).
        receipt = self._receipt()
        first = JOIN.canonical_receipt_digest(receipt)
        second = JOIN.canonical_receipt_digest(json.loads(json.dumps(receipt)))
        self.assertEqual(first, second)

    def test_b_digest_unaffected_by_one_sided_timing_stamp(self):
        receipt = self._receipt()
        untouched = JOIN.canonical_receipt_digest(receipt)
        stamped = JOIN.stamp_delivery_receipt(receipt, "join_completed_ns", at_ns=123)
        self.assertIn("delivery_timing", stamped)
        self.assertNotIn("delivery_timing", receipt)
        self.assertEqual(untouched, JOIN.canonical_receipt_digest(stamped))

    def test_c_armed_and_out_of_vocabulary_keys_never_reach_the_canonical_body(self):
        receipt = self._receipt(
            armed="registry",
            launch_home="/home/example",
            harvest_command="python3 utilities/dispatch-registry.py --harvest",
        )
        canonical = JOIN.canonical_delivery_receipt(receipt)
        self.assertNotIn("armed", canonical)
        self.assertNotIn("launch_home", canonical)
        self.assertNotIn("harvest_command", canonical)
        self.assertEqual(set(canonical) - {"children"}, JOIN.CANONICAL_RECEIPT_KEYS - {"children"})
        body = json.dumps(canonical, separators=(",", ":"), sort_keys=True)
        self.assertNotIn("armed=", body)

    def test_d_seal_unseal_round_trips_the_worst_case_2048_byte_body(self):
        # Build a receipt whose canonical JSON encoding is exactly
        # MAX_DELIVERY_RECEIPT_BYTES (2048) UTF-8 bytes -- SD-108's own limit
        # -- by padding one child's `reason` (an ALLOWED_REASONS-typed field
        # in production, but seal/unseal never validates receipt semantics,
        # only bytes) with filler up to the exact byte target.
        receipt = self._receipt()
        encoded_without_filler = json.dumps(
            receipt, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
        filler_len = JOIN.MAX_DELIVERY_RECEIPT_BYTES - len(encoded_without_filler)
        self.assertGreater(filler_len, 0)
        receipt["job_registry"] = receipt["job_registry"] + ("x" * filler_len)
        body_bytes = json.dumps(receipt, separators=(",", ":"), sort_keys=True).encode("utf-8")
        self.assertEqual(len(body_bytes), JOIN.MAX_DELIVERY_RECEIPT_BYTES)

        sealed = JOIN.seal_delivery_receipt(receipt)
        self.assertNotIn("=", sealed)
        self.assertNotIn("(", sealed)
        self.assertNotIn(",", sealed)
        self.assertFalse(any(c.isspace() for c in sealed))

        with tempfile.TemporaryDirectory() as tmp:
            jobs = Path(tmp) / "jobs.log"
            digest = JOIN.canonical_receipt_digest(receipt)
            pipe = ",".join([
                "attempt_id=att-0000000000000000000000000000aaaa",
                "delivery_intent=1",
                f"delivery_receipt_digest={digest}",
                f"delivery_receipt_b64={sealed}",
                "note=completed-marker",
            ])
            line = "\t".join(["done", "done", "/repo", "/wt", "slug", pipe])
            jobs.write_text(line + "\n", encoding="utf-8")

            reread = jobs.read_text(encoding="utf-8").splitlines()
            fields = reread[0].split("\t")
            metadata = D.parse_registry_metadata(fields[5])
            self.assertEqual(metadata["delivery_receipt_b64"], sealed)

            restored = JOIN.unseal_delivery_receipt(metadata["delivery_receipt_b64"])
            self.assertEqual(restored, receipt)
            self.assertEqual(JOIN.canonical_receipt_digest(restored), digest)

        tools_dir = str(HERE.parent / "tools")
        if tools_dir not in sys.path:
            sys.path.insert(0, tools_dir)
        from fleet.collectors import dispatch as fleet_dispatch  # noqa: E402

        fleet_meta = fleet_dispatch._parse_pipe_meta(pipe)
        self.assertEqual(fleet_meta.get("delivery_receipt_b64"), sealed)
        self.assertEqual(fleet_meta.get("delivery_receipt_digest"), digest)
        self.assertEqual(fleet_meta.get("delivery_intent"), "1")

    def test_oversized_receipt_body_is_refused_before_sealing(self):
        receipt = self._receipt(job_registry="x" * JOIN.MAX_DELIVERY_RECEIPT_BYTES)
        with self.assertRaises(JOIN.JoinContractError):
            JOIN.seal_delivery_receipt(receipt)

    def test_unseal_rejects_malformed_input(self):
        with self.assertRaises(JOIN.JoinContractError):
            JOIN.unseal_delivery_receipt("")
        with self.assertRaises(JOIN.JoinContractError):
            JOIN.unseal_delivery_receipt("not base64!!")
        with self.assertRaises(JOIN.JoinContractError):
            JOIN.unseal_delivery_receipt(base64.standard_b64encode(b"[1,2]").decode("ascii"))


class MaterializePendingDeliveryTest(unittest.TestCase):
    """SD-111 P2 round 2 C-1: carrier-independent idempotent materializer.

    The invariant under test: after a delivery-owing terminal transition
    commits, a pending record exists even though no carrier (rewake hook,
    session sweep) ever ran -- this suite never imports or calls one.
    """

    CURRENT_METADATA = (
        "attempt_schema_version=2,dispatch_depth=2,transport=headless,"
        "execution_surface=registered-headless,registered_worker=1,"
        "fallback_hop=same-harness-headless"
    )

    def _row(self, attempt="att-materialize-fixture"):
        pipe = ",".join([
            self.CURRENT_METADATA,
            f"attempt_id={attempt}",
            "parent_attempt_id=att-materialize-parent",
            "parent_completion_delivery=claude-parent-runtime",
            "parent_sid=sess-materialize-fixture",
            "route_id=rt-materialize-fixture",
            "route_node=execute",
            "harness=claude",
        ])
        return f"2026-08-28T00:00:00Z\topen\t/r\t/w\texecute\t{pipe}"

    def _close_and_get_fields(self, jobs, attempt="att-materialize-fixture"):
        self.assertTrue(D.close_attempt_row(jobs, attempt, "completed-marker"))
        line = jobs.read_text(encoding="utf-8").splitlines()[0]
        return line.split("\t")

    def test_materialize_creates_record_from_intent_stamped_row(self):
        with tempfile.TemporaryDirectory() as td:
            jobs = Path(td) / "jobs.log"
            jobs.write_text(self._row() + "\n", encoding="utf-8")
            fields = self._close_and_get_fields(jobs)
            path = JOIN.materialize_pending_delivery(jobs, fields)
            self.assertIsNotNone(path)
            self.assertTrue(path.is_file())
            record = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(record["state"], "pending")
            self.assertEqual(record["attempts"], 0)
            self.assertEqual(record["recipient_kind"], "claude-parent-runtime")
            self.assertEqual(record["attempt_ids"], ["att-materialize-fixture"])
            self.assertEqual(
                record["receipt"]["children"][0]["attempt_id"], "att-materialize-fixture"
            )

    OWNER_METADATA = (
        "attempt_schema_version=2,dispatch_depth=1,transport=headless,"
        "execution_surface=registered-headless,registered_worker=1,"
        "fallback_hop=same-harness-headless,worker_type=owner,unit=_kernel/owner"
    )

    def _owner_row(self, attempt="att-materialize-owner"):
        # Real dispatch-depth-1 owner shape (2026-08-29 mega-audit-af row):
        # bound through owner_route_id, no route_id/route_node, no parent attempt.
        pipe = ",".join([
            self.OWNER_METADATA,
            f"attempt_id={attempt}",
            "parent_attempt_id=-",
            "parent_completion_delivery=claude-parent-runtime",
            "parent_sid=sess-materialize-owner",
            "owner_route_id=rt-materialize-owner",
            "owner_route_hash=sha256:owner",
            "harness=claude",
        ])
        return f"2026-08-28T00:00:00Z\topen\t/r\t/w\towner-slug\t{pipe}"

    def test_materialize_owner_row_resolves_identity_from_owner_route(self):
        # Regression: before 2026-08-29 every owner terminal was refused as
        # identity-incomplete (route_id/route_node empty) and the depth-0
        # session never received a pending record.
        with tempfile.TemporaryDirectory() as td:
            jobs = Path(td) / "jobs.log"
            jobs.write_text(self._owner_row() + "\n", encoding="utf-8")
            fields = self._close_and_get_fields(jobs, "att-materialize-owner")
            self.assertEqual(
                JOIN.pending_record_identity(D.parse_registry_metadata(fields[5])),
                ("rt-materialize-owner", JOIN.OWNER_ROUTE_NODE, JOIN.NO_PARENT_ATTEMPT),
            )
            path = JOIN.materialize_pending_delivery(jobs, fields)
            self.assertIsNotNone(path)
            record = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(record["route_id"], "rt-materialize-owner")
            self.assertEqual(record["route_node"], "_owner")
            self.assertEqual(record["parent_attempt_id"], "-")
            self.assertEqual(record["recipient_kind"], "claude-parent-runtime")
            self.assertEqual(record["state"], "pending")

    def test_stage_row_identity_is_unchanged_by_owner_resolution(self):
        with tempfile.TemporaryDirectory() as td:
            jobs = Path(td) / "jobs.log"
            jobs.write_text(self._row() + "\n", encoding="utf-8")
            fields = self._close_and_get_fields(jobs)
            self.assertEqual(
                JOIN.pending_record_identity(D.parse_registry_metadata(fields[5])),
                ("rt-materialize-fixture", "execute", "att-materialize-parent"),
            )

    def test_refused_materialize_leaves_a_durable_log_line(self):
        # A depth-2 row with no route identity is still refused, but the
        # refusal must now be traceable after the fact.
        with tempfile.TemporaryDirectory() as td:
            jobs = Path(td) / "jobs.log"
            pipe = ",".join([
                self.CURRENT_METADATA,
                "attempt_id=att-materialize-broken",
                "parent_attempt_id=att-materialize-parent",
                "parent_completion_delivery=claude-parent-runtime",
                "parent_sid=sess-materialize-fixture",
                "harness=claude",
            ])
            jobs.write_text(
                f"2026-08-28T00:00:00Z\topen\t/r\t/w\texecute\t{pipe}\n", encoding="utf-8"
            )
            self.assertTrue(D.close_attempt_row(jobs, "att-materialize-broken", "completed-marker"))
            self.assertIsNone(JOIN.materialize_after_terminal_close(jobs, "att-materialize-broken"))
            log = Path(td) / "logs" / JOIN.PENDING_DELIVERY_LOG
            self.assertTrue(log.is_file())
            entry = json.loads(log.read_text(encoding="utf-8").splitlines()[-1])
            self.assertEqual(entry["event"], "delivery-persistence-refused")
            self.assertEqual(entry["attempt_id"], "att-materialize-broken")
            self.assertIn("identity-incomplete", entry["reason"])

    def test_materialize_returns_none_without_delivery_intent(self):
        with tempfile.TemporaryDirectory() as td:
            jobs = Path(td) / "jobs.log"
            pipe = f"{self.CURRENT_METADATA},attempt_id=att-no-recipient"
            jobs.write_text(
                f"2026-08-28T00:00:00Z\topen\t/r\t/w\texecute\t{pipe}\n", encoding="utf-8"
            )
            self.assertTrue(D.close_attempt_row(jobs, "att-no-recipient", "completed-marker"))
            fields = jobs.read_text(encoding="utf-8").splitlines()[0].split("\t")
            self.assertIsNone(JOIN.materialize_pending_delivery(jobs, fields))

    def test_trigger_1_then_trigger_2_converge_on_one_record(self):
        # Trigger 1 (in-process, right after lock release) and trigger 2
        # (reconcile backstop) both call this same function; N calls for one
        # row must produce exactly one file (round 2 C-1).
        with tempfile.TemporaryDirectory() as td:
            jobs = Path(td) / "jobs.log"
            jobs.write_text(self._row() + "\n", encoding="utf-8")
            fields = self._close_and_get_fields(jobs)
            first_path = JOIN.materialize_pending_delivery(jobs, fields)
            second_path = JOIN.materialize_pending_delivery(jobs, fields)
            self.assertEqual(first_path, second_path)
            directory = first_path.parent
            self.assertEqual(len(list(directory.glob("*.json"))), 1)

    def test_trigger_2_alone_recovers_from_a_skipped_trigger_1_crash(self):
        # Model the crash window explicitly: the row closed (terminal intent
        # committed) but the in-process trigger 1 call never happened (as if
        # the launcher died right after releasing the lock). Trigger 2 -- the
        # reconcile backstop, modeled here as the sole materialize call --
        # must still produce the record on its own.
        with tempfile.TemporaryDirectory() as td:
            jobs = Path(td) / "jobs.log"
            jobs.write_text(self._row() + "\n", encoding="utf-8")
            fields = self._close_and_get_fields(jobs)
            # Trigger 1 deliberately skipped here.
            path = JOIN.materialize_pending_delivery(jobs, fields)
            self.assertIsNotNone(path)
            self.assertTrue(path.is_file())

    def test_zero_carrier_materialize_invariant_no_claim_no_emit(self):
        # §4.4's core invariant, restated as a test: a pending record exists
        # after the terminal transition even though no carrier ran. This test
        # imports neither hooks/dispatch-owner-rewake.py nor
        # dispatch_session_sweep -- there is no carrier import to call.
        with tempfile.TemporaryDirectory() as td:
            jobs = Path(td) / "jobs.log"
            jobs.write_text(self._row() + "\n", encoding="utf-8")
            fields = self._close_and_get_fields(jobs)
            path = JOIN.materialize_pending_delivery(jobs, fields)
            record = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(record["state"], "pending")
            self.assertIsNone(record["claim_owner"])
            self.assertIsNone(record["claimed_at_ns"])
            self.assertEqual(record["attempts"], 0)

    def test_identity_conflict_receipt_digest_mismatch_fails_closed(self):
        with tempfile.TemporaryDirectory() as td:
            jobs = Path(td) / "jobs.log"
            jobs.write_text(self._row() + "\n", encoding="utf-8")
            fields = self._close_and_get_fields(jobs)
            metadata = D.parse_registry_metadata(fields[5])
            corrupted = dict(metadata)
            corrupted["delivery_receipt_digest"] = "0" * 64
            pipe = ",".join(f"{k}={v}" for k, v in corrupted.items())
            fields[5] = pipe
            import dispatch_pending_delivery as PD  # noqa: E402
            with self.assertRaises(PD.PendingDeliveryError) as ctx:
                JOIN.materialize_pending_delivery(jobs, fields)
            self.assertEqual(ctx.exception.reason, "pending-delivery-identity-conflict")

    def _owner_row_variant(
        self, attempt, *, worker_type, dispatch_depth, owner_route_id="rt-a47-3-owner"
    ):
        # A47-3: exercise the `worker_type=owner OR dispatch_depth=1` gate as
        # an OR, not an AND -- one field set, the other left off its owner
        # value, still must resolve through owner_route_id.
        fields = [
            f"attempt_schema_version=2,dispatch_depth={dispatch_depth},"
            "transport=headless,execution_surface=registered-headless,"
            f"registered_worker=1,fallback_hop=same-harness-headless,"
            f"worker_type={worker_type},unit=_kernel/owner",
            f"attempt_id={attempt}",
            "parent_attempt_id=-",
            "parent_completion_delivery=claude-parent-runtime",
            f"parent_sid=sess-{attempt}",
            "harness=claude",
        ]
        if owner_route_id:
            fields.insert(-1, f"owner_route_id={owner_route_id}")
            fields.insert(-1, "owner_route_hash=sha256:owner")
        pipe = ",".join(fields)
        return f"2026-08-28T00:00:00Z\topen\t/r\t/w\towner-slug\t{pipe}"

    def test_a47_3_owner_row_identity_sentinels(self):
        # Predicate: for every terminal row with worker_type=owner OR
        # dispatch_depth=1, record.route_id == row.owner_route_id,
        # record.route_node == "_owner", record.parent_attempt_id == "-" --
        # and a plain dispatch-depth-2 stage row's identity is untouched.
        with tempfile.TemporaryDirectory() as td:
            jobs = Path(td) / "jobs.log"

            # (a) worker_type=owner alone (dispatch_depth left at 2).
            attempt_a = "att-a47-3-workertype-owner"
            jobs.write_text(
                self._owner_row_variant(
                    attempt_a,
                    worker_type="owner",
                    dispatch_depth="2",
                    owner_route_id="rt-a47-3-owner-a",
                )
                + "\n",
                encoding="utf-8",
            )
            fields_a = self._close_and_get_fields(jobs, attempt_a)
            metadata_a = D.parse_registry_metadata(fields_a[5])
            self.assertEqual(
                JOIN.pending_record_identity(metadata_a),
                ("rt-a47-3-owner-a", JOIN.OWNER_ROUTE_NODE, JOIN.NO_PARENT_ATTEMPT),
            )
            record_a = json.loads(
                JOIN.materialize_pending_delivery(jobs, fields_a).read_text(encoding="utf-8")
            )
            self.assertEqual(record_a["route_id"], "rt-a47-3-owner-a")
            self.assertEqual(record_a["route_node"], JOIN.OWNER_ROUTE_NODE)
            self.assertEqual(record_a["parent_attempt_id"], JOIN.NO_PARENT_ATTEMPT)

            # (b) dispatch_depth=1 alone (worker_type left off "owner").
            attempt_b = "att-a47-3-depth-one"
            jobs.write_text(
                self._owner_row_variant(
                    attempt_b,
                    worker_type="quick",
                    dispatch_depth="1",
                    owner_route_id="rt-a47-3-owner-b",
                )
                + "\n",
                encoding="utf-8",
            )
            fields_b = self._close_and_get_fields(jobs, attempt_b)
            metadata_b = D.parse_registry_metadata(fields_b[5])
            self.assertEqual(
                JOIN.pending_record_identity(metadata_b),
                ("rt-a47-3-owner-b", JOIN.OWNER_ROUTE_NODE, JOIN.NO_PARENT_ATTEMPT),
            )
            record_b = json.loads(
                JOIN.materialize_pending_delivery(jobs, fields_b).read_text(encoding="utf-8")
            )
            self.assertEqual(record_b["route_id"], "rt-a47-3-owner-b")
            self.assertEqual(record_b["route_node"], JOIN.OWNER_ROUTE_NODE)
            self.assertEqual(record_b["parent_attempt_id"], JOIN.NO_PARENT_ATTEMPT)

            # (c) an ordinary stage row's identity is unchanged by any of the
            # owner resolution above.
            jobs.write_text(self._row() + "\n", encoding="utf-8")
            stage_fields = self._close_and_get_fields(jobs)
            stage_metadata = D.parse_registry_metadata(stage_fields[5])
            self.assertEqual(
                JOIN.pending_record_identity(stage_metadata),
                ("rt-materialize-fixture", "execute", "att-materialize-parent"),
            )

    def test_a47_3_missing_owner_route_id_refused(self):
        # Predicate: an owner-identified row with no owner_route_id resolves
        # to route_id="", so `create()`'s identity check refuses it -- zero
        # records, one typed identity-incomplete refusal.
        with tempfile.TemporaryDirectory() as td:
            jobs = Path(td) / "jobs.log"
            attempt = "att-a47-3-missing-owner-route"
            jobs.write_text(
                self._owner_row_variant(
                    attempt, worker_type="owner", dispatch_depth="1", owner_route_id=None
                )
                + "\n",
                encoding="utf-8",
            )
            fields = self._close_and_get_fields(jobs, attempt)
            metadata = D.parse_registry_metadata(fields[5])
            self.assertEqual(
                JOIN.pending_record_identity(metadata),
                ("", JOIN.OWNER_ROUTE_NODE, JOIN.NO_PARENT_ATTEMPT),
            )
            import dispatch_pending_delivery as PD  # noqa: E402

            with self.assertRaises(PD.PendingDeliveryError) as ctx:
                JOIN.materialize_pending_delivery(jobs, fields)
            self.assertEqual(ctx.exception.reason, "pending-delivery-identity-conflict")
            self.assertEqual(ctx.exception.detail, "identity-incomplete")

            self.assertIsNone(JOIN.materialize_after_terminal_close(jobs, attempt))
            record_dir = Path(td) / "pending-delivery"
            records = list(record_dir.rglob("*.json")) if record_dir.is_dir() else []
            self.assertEqual(len(records), 0)
            log = Path(td) / "logs" / JOIN.PENDING_DELIVERY_LOG
            entries = [
                json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()
            ]
            refusals = [e for e in entries if e["attempt_id"] == attempt]
            self.assertEqual(len(refusals), 1)
            self.assertIn("identity-incomplete", refusals[0]["reason"])

    def test_owner_row_identity_resolves_verified_advance_over_sealed_route_id(self):
        # An owner may have advanced past its launch-sealed owner_route_id;
        # when a jobs path is supplied the verified current binding must win.
        import importlib.util as _ilu
        spec = _ilu.spec_from_file_location(
            "owner_route_binding_ocj", HERE / "owner_route_binding.py"
        )
        orb = _ilu.module_from_spec(spec)
        sys.modules[spec.name] = orb
        spec.loader.exec_module(orb)
        with tempfile.TemporaryDirectory() as td:
            jobs = Path(td) / "jobs.log"
            metadata = {
                "worker_type": "owner", "dispatch_depth": "1",
                "attempt_id": "att-advance-owner",
                "owner_route_file": str(Path(td) / "r0.json"),
                "owner_route_id": "rt-sealed-r0",
                "owner_route_hash": "sha256:r0",
            }
            current = orb.OwnerRouteBinding(str(Path(td) / "r1.json"), "rt-current-r1", "sha256:r1")
            with mock.patch.object(orb, "resolve_owner_route_lifecycle",
                                   return_value=(current, "owner-route-advance-current")), \
                 mock.patch.dict(sys.modules, {"owner_route_binding": orb}):
                result = JOIN.pending_record_identity(metadata, jobs)
            self.assertEqual(result, ("rt-current-r1", JOIN.OWNER_ROUTE_NODE, JOIN.NO_PARENT_ATTEMPT))

    def test_owner_row_identity_preserves_sealed_route_id_when_advance_absent(self):
        import importlib.util as _ilu
        spec = _ilu.spec_from_file_location(
            "owner_route_binding_ocj2", HERE / "owner_route_binding.py"
        )
        orb = _ilu.module_from_spec(spec)
        sys.modules[spec.name] = orb
        spec.loader.exec_module(orb)
        with tempfile.TemporaryDirectory() as td:
            jobs = Path(td) / "jobs.log"
            metadata = {
                "worker_type": "owner", "dispatch_depth": "1",
                "attempt_id": "att-legacy-owner",
                "owner_route_file": str(Path(td) / "r0.json"),
                "owner_route_id": "rt-sealed-r0",
                "owner_route_hash": "sha256:r0",
            }
            anchor = orb.OwnerRouteBinding(str(Path(td) / "r0.json"), "rt-sealed-r0", "sha256:r0")
            with mock.patch.object(orb, "resolve_owner_route_lifecycle",
                                   return_value=(anchor, "owner-route-launch-binding")), \
                 mock.patch.dict(sys.modules, {"owner_route_binding": orb}):
                result = JOIN.pending_record_identity(metadata, jobs)
            self.assertEqual(result, ("rt-sealed-r0", JOIN.OWNER_ROUTE_NODE, JOIN.NO_PARENT_ATTEMPT))

    def test_owner_row_identity_resolves_post_launch_attachment_without_sealed_tuple(self):
        import importlib.util as _ilu
        spec = _ilu.spec_from_file_location(
            "owner_route_binding_ocj_attach", HERE / "owner_route_binding.py"
        )
        orb = _ilu.module_from_spec(spec)
        sys.modules[spec.name] = orb
        spec.loader.exec_module(orb)
        with tempfile.TemporaryDirectory() as td:
            jobs = Path(td) / "jobs.log"
            metadata = {
                "worker_type": "owner", "dispatch_depth": "1",
                "attempt_id": "att-attached-owner",
            }
            current = orb.OwnerRouteBinding(
                str(Path(td) / "r0.json"), "rt-attached-r0", "sha256:r0"
            )
            with mock.patch.object(
                orb, "resolve_owner_route_lifecycle",
                return_value=(current, "owner-route-post-launch-attachment"),
            ), mock.patch.dict(sys.modules, {"owner_route_binding": orb}):
                result = JOIN.pending_record_identity(metadata, jobs)
            self.assertEqual(
                result, ("rt-attached-r0", JOIN.OWNER_ROUTE_NODE, JOIN.NO_PARENT_ATTEMPT)
            )

    def test_owner_row_identity_refuses_on_invalid_advance_evidence(self):
        import importlib.util as _ilu
        spec = _ilu.spec_from_file_location(
            "owner_route_binding_ocj3", HERE / "owner_route_binding.py"
        )
        orb = _ilu.module_from_spec(spec)
        sys.modules[spec.name] = orb
        spec.loader.exec_module(orb)
        with tempfile.TemporaryDirectory() as td:
            jobs = Path(td) / "jobs.log"
            metadata = {
                "worker_type": "owner", "dispatch_depth": "1",
                "attempt_id": "att-tampered-owner",
                "owner_route_file": str(Path(td) / "r0.json"),
                "owner_route_id": "rt-sealed-r0",
                "owner_route_hash": "sha256:r0",
            }
            with mock.patch.object(
                orb, "resolve_owner_route_lifecycle",
                side_effect=orb.OwnerRouteBindingError("owner-route-advance-target-invalid"),
            ), mock.patch.dict(sys.modules, {"owner_route_binding": orb}):
                with self.assertRaisesRegex(JOIN.JoinContractError, "owner-route-advance-conflict"):
                    JOIN.pending_record_identity(metadata, jobs)

    def test_a47_9_persistence_refusal_observed_row_close_unchanged(self):
        # Predicate: forcing N materialize failures produces exactly N
        # observed `delivery-persistence-refused` log entries, and none of
        # them change whether the underlying row-close already succeeded --
        # row status stays "done" (delta 0) regardless of the persistence
        # outcome downstream of it.
        with tempfile.TemporaryDirectory() as td:
            jobs = Path(td) / "jobs.log"
            attempts = ["att-a47-9-forced-a", "att-a47-9-forced-b"]
            rows = "\n".join(self._row(attempt) for attempt in attempts) + "\n"
            jobs.write_text(rows, encoding="utf-8")
            for attempt in attempts:
                self.assertTrue(D.close_attempt_row(jobs, attempt, "completed-marker"))

            def _row_status(attempt):
                for line in jobs.read_text(encoding="utf-8").splitlines():
                    fields = line.split("\t")
                    if D.parse_registry_metadata(fields[5]).get("attempt_id") == attempt:
                        return fields[1]
                return None

            pre_status = {attempt: _row_status(attempt) for attempt in attempts}
            self.assertEqual(set(pre_status.values()), {"done"})

            import dispatch_pending_delivery as PD  # noqa: E402

            with mock.patch.object(
                JOIN.pending_delivery,
                "create",
                side_effect=PD.PendingDeliveryError("delivery-persistence-refused", "forced"),
            ):
                results = [
                    JOIN.materialize_after_terminal_close(jobs, attempt)
                    for attempt in attempts
                ]
            self.assertEqual(results, [None, None])

            post_status = {attempt: _row_status(attempt) for attempt in attempts}
            self.assertEqual(pre_status, post_status)

            log = Path(td) / "logs" / JOIN.PENDING_DELIVERY_LOG
            entries = [
                json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()
            ]
            forced_refusals = [
                e
                for e in entries
                if e["event"] == "delivery-persistence-refused" and e["attempt_id"] in attempts
            ]
            self.assertEqual(len(forced_refusals), len(attempts))
            for entry in forced_refusals:
                self.assertIn("forced", entry["reason"])


class ReconcilePendingDeliveryTest(unittest.TestCase):
    """SD-111 P2 trigger 2 + expiry actor (§2-b-2/§2-c).

    ``reconcile_pending_delivery`` is the bounded-cadence backstop wired into
    `dispatch-registry.py reconcile --apply` -- it is exercised here directly
    against a synthetic ``jobs.log`` + pending-delivery tree, never through a
    live registry.
    """

    CURRENT_METADATA = (
        "attempt_schema_version=2,dispatch_depth=1,transport=headless,"
        "execution_surface=registered-headless,registered_worker=1,"
        "fallback_hop=same-harness-headless"
    )

    def _row(self, attempt, *, ancestry=None):
        pipe = ",".join(filter(None, [
            self.CURRENT_METADATA,
            f"attempt_id={attempt}",
            "parent_attempt_id=att-reconcile-parent",
            "parent_completion_delivery=claude-parent-runtime",
            f"parent_sid=sess-{attempt}",
            "route_id=rt-reconcile-fixture",
            "route_node=execute",
            "harness=claude",
            (
                f"parent_runtime_pid={ancestry[0]},parent_runtime_pid_start={ancestry[1]}"
                if ancestry else None
            ),
        ]))
        return f"2026-08-28T00:00:00Z\topen\t/r\t/w\texecute\t{pipe}"

    def test_materializes_a_row_intent_stamped_but_not_yet_materialized(self):
        with tempfile.TemporaryDirectory() as td:
            jobs = Path(td) / "jobs.log"
            jobs.write_text(self._row("att-reconcile-a") + "\n", encoding="utf-8")
            self.assertTrue(D.close_attempt_row(jobs, "att-reconcile-a", "completed-marker"))
            # Trigger 1 deliberately skipped -- models the crash window.
            result = JOIN.reconcile_pending_delivery(jobs)
            self.assertEqual(result["materialized"], 1)
            root = jobs.resolve(strict=False).parent
            self.assertEqual(len(list((root / "pending-delivery").glob("*/*.json"))), 1)

    def test_second_pass_is_a_no_op(self):
        with tempfile.TemporaryDirectory() as td:
            jobs = Path(td) / "jobs.log"
            jobs.write_text(self._row("att-reconcile-b") + "\n", encoding="utf-8")
            self.assertTrue(D.close_attempt_row(jobs, "att-reconcile-b", "completed-marker"))
            first = JOIN.reconcile_pending_delivery(jobs)
            self.assertEqual(first["materialized"], 1)
            second = JOIN.reconcile_pending_delivery(jobs)
            self.assertEqual(second["materialized"], 0)
            root = jobs.resolve(strict=False).parent
            self.assertEqual(len(list((root / "pending-delivery").glob("*/*.json"))), 1)

    def test_expires_a_record_whose_owning_runtime_process_is_provably_dead(self):
        with tempfile.TemporaryDirectory() as td:
            jobs = Path(td) / "jobs.log"
            dead_ancestry = ("999999999", "123456789")
            jobs.write_text(
                self._row("att-reconcile-c", ancestry=dead_ancestry) + "\n", encoding="utf-8"
            )
            self.assertTrue(D.close_attempt_row(jobs, "att-reconcile-c", "completed-marker"))
            # Materialize and expire both happen in the same pass here --
            # the freshly materialized record is already on disk by the time
            # this call's own expiry sweep runs.
            first = JOIN.reconcile_pending_delivery(jobs)
            self.assertEqual(first["materialized"], 1)
            self.assertEqual(first["expired"], 1)
            root = jobs.resolve(strict=False).parent
            record_file = next((root / "pending-delivery").glob("*/*.json"))
            record = json.loads(record_file.read_text(encoding="utf-8"))
            self.assertEqual(record["state"], "expired")
            self.assertEqual(record["expiry_reason"], "recipient-session-gone")
            # Expired records are never deleted (§10.2).
            self.assertTrue(record_file.is_file())

    def test_never_expires_a_record_whose_owning_process_is_alive(self):
        with tempfile.TemporaryDirectory() as td:
            jobs = Path(td) / "jobs.log"
            alive_ancestry = (str(os.getpid()), D.process_start_ticks(os.getpid()))
            jobs.write_text(
                self._row("att-reconcile-d", ancestry=alive_ancestry) + "\n", encoding="utf-8"
            )
            self.assertTrue(D.close_attempt_row(jobs, "att-reconcile-d", "completed-marker"))
            JOIN.reconcile_pending_delivery(jobs)
            second = JOIN.reconcile_pending_delivery(jobs)
            self.assertEqual(second["expired"], 0)
            root = jobs.resolve(strict=False).parent
            record_file = next((root / "pending-delivery").glob("*/*.json"))
            record = json.loads(record_file.read_text(encoding="utf-8"))
            self.assertEqual(record["state"], "pending")

    def test_never_expires_a_record_whose_owning_process_is_only_inaccessible(self):
        # F-1 regression: a permission/namespace/procfs denial ("inaccessible")
        # must not be conflated with "missing" and expire a live session's
        # pending record. `_proc_observation` is dispatch_contract's own
        # distinguishing primitive (PermissionError -> "inaccessible" vs
        # FileNotFoundError -> "missing"); patching it directly is the exact
        # boundary the review's suggested fix names.
        with tempfile.TemporaryDirectory() as td:
            jobs = Path(td) / "jobs.log"
            ancestry = ("424242", "111222333")
            jobs.write_text(
                self._row("att-reconcile-f", ancestry=ancestry) + "\n", encoding="utf-8"
            )
            self.assertTrue(D.close_attempt_row(jobs, "att-reconcile-f", "completed-marker"))
            # Patch from the very first pass: the fixture pid is synthetic
            # and not a real process, so an unpatched real `_proc_observation`
            # would itself report "missing" and mask the regression this
            # test is for.
            with mock.patch.object(
                D, "_proc_observation", return_value=("inaccessible", "", "")
            ):
                first = JOIN.reconcile_pending_delivery(jobs)
                self.assertEqual(first["materialized"], 1)
                second = JOIN.reconcile_pending_delivery(jobs)
            self.assertEqual(second["expired"], 0)
            root = jobs.resolve(strict=False).parent
            record_file = next((root / "pending-delivery").glob("*/*.json"))
            record = json.loads(record_file.read_text(encoding="utf-8"))
            self.assertEqual(record["state"], "pending")

    def test_never_expires_when_ancestry_fields_are_absent(self):
        with tempfile.TemporaryDirectory() as td:
            jobs = Path(td) / "jobs.log"
            jobs.write_text(self._row("att-reconcile-e") + "\n", encoding="utf-8")
            self.assertTrue(D.close_attempt_row(jobs, "att-reconcile-e", "completed-marker"))
            first = JOIN.reconcile_pending_delivery(jobs)
            self.assertEqual(first["materialized"], 1)
            second = JOIN.reconcile_pending_delivery(jobs)
            self.assertEqual(second["expired"], 0)
            self.assertEqual(second["skipped"], 1)
            root = jobs.resolve(strict=False).parent
            record_file = next((root / "pending-delivery").glob("*/*.json"))
            record = json.loads(record_file.read_text(encoding="utf-8"))
            self.assertEqual(record["state"], "pending")

    def test_no_delivery_rows_is_a_clean_no_op(self):
        with tempfile.TemporaryDirectory() as td:
            jobs = Path(td) / "jobs.log"
            jobs.write_text("fixture\n", encoding="utf-8")
            result = JOIN.reconcile_pending_delivery(jobs)
            self.assertEqual(result, {"materialized": 0, "expired": 0, "skipped": 0})


if __name__ == "__main__":
    unittest.main()
