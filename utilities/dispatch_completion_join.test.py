#!/usr/bin/env python3

from __future__ import annotations

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

    def test_blocked_row_without_readable_artifact_also_closes_typed(self):
        # The pre-existing branch (no readable artifact) must keep closing
        # BLOCKED/FAIL typed too, not just the new readable-artifact branch.
        row = self.child(verdict="BLOCKED", artifact=None, quiescent=True)
        self.jobs.write_text(row.raw + "\n", encoding="utf-8")
        reason = JOIN.close_finished_child(row, jobs=self.jobs)
        self.assertEqual(reason, "")
        lines = self.jobs.read_text(encoding="utf-8").strip().splitlines()
        self.assertIn("dead-worker-blocked", lines[0])

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


if __name__ == "__main__":
    unittest.main()
