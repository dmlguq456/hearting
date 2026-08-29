import tempfile
from pathlib import Path
import unittest
from unittest import mock

from tools.fleet.collectors import dispatch, procscan
from tools.fleet.model import DispatchJob, Session


CURRENT = (
    "attempt_schema_version=2,transport=headless,"
    "execution_surface=registered-headless,registered_worker=1,"
    "fallback_hop=same-harness-headless"
)


def registry_row(status, attempt, *, route_id="rt-f40", route_node="one-shot",
                 slug="stage-worker", prefix=CURRENT, parent=None,
                 parent_attempt_id=None):
    owner = ""
    if parent is not None:
        owner += f",parent={parent}"
    if parent_attempt_id is not None:
        owner += f",parent_attempt_id={parent_attempt_id}"
    pipe = (
        f"{prefix},dispatch_depth=2,capability=autopilot-code,"
        f"harness=claude,route_id={route_id},route_node={route_node},"
        f"attempt_id={attempt}{owner}"
    )
    return (
        f"2026-07-24T00:00:00+00:00\t{status}\t/repo\t/work/shared\t"
        f"{slug}\t{pipe}\n"
    )


def proc_job(attempt, *, route_id="rt-f40", route_node="one-shot",
             cwd="/work/shared", source="proc"):
    return DispatchJob(
        key="code", slug="stage-worker", cwd=cwd, source=source,
        harness="claude", dispatch_depth=2, depth=2,
        route_id=route_id, route_node=route_node, attempt_id=attempt,
    )


class ProcessAttemptProjectionTest(unittest.TestCase):
    def test_session_projects_attempt_without_reclassifying_from_attempt_alone(self):
        env = {"AGENT_DISPATCH_ATTEMPT_ID": "att-session-exact"}
        with mock.patch.object(
                procscan, "_ps_lines",
                return_value=["123 codex 00:05 /usr/bin/codex exec"]), \
             mock.patch.object(procscan, "_read_cwd",
                               return_value=("/work/shared", False)), \
             mock.patch.object(procscan, "_pid_ttys", return_value={}), \
             mock.patch.object(procscan, "_detached_ttys", return_value=set()), \
             mock.patch.object(procscan, "_is_detached", return_value=False), \
             mock.patch.object(procscan, "read_environ", return_value=env), \
             mock.patch.object(procscan, "read_proc_start", return_value="start-123"):
            sessions = procscan.scan()
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0].attempt_id, "att-session-exact")
        self.assertFalse(sessions[0].is_child)

    def test_proc_dispatch_job_projects_exact_attempt(self):
        env = {
            "AGENT_DISPATCH_ATTEMPT_ID": "att-proc-exact",
            "AGENT_DISPATCH_ATTEMPT_SCHEMA_VERSION": "2",
            "AGENT_DISPATCH_DEPTH": "2",
            "AGENT_DISPATCH_TRANSPORT": "headless",
            "AGENT_DISPATCH_EXECUTION_SURFACE": "registered-headless",
            "AGENT_DISPATCH_REGISTERED_WORKER": "1",
            "AGENT_DISPATCH_FALLBACK_HOP": "same-harness-headless",
            "AGENT_ROUTE_ID": "rt-f40",
            "AGENT_ROUTE_NODE": "one-shot",
        }
        line = "321 claude 00:05 /usr/bin/claude /autopilot-code --mode dev"
        with mock.patch.object(procscan, "_ps_lines", return_value=[line]), \
             mock.patch.object(procscan, "read_environ", return_value=env), \
             mock.patch.object(procscan, "read_proc_start", return_value="start-321"), \
             mock.patch.object(dispatch.os, "readlink", return_value="/work/shared"), \
             mock.patch.object(dispatch, "_claude_job_model", return_value="Sonnet"):
            jobs = dispatch._scan_processes()
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].attempt_id, "att-proc-exact")
        self.assertEqual(jobs[0].attempt_contract_status, "current")


class TerminalAttemptIndexTest(unittest.TestCase):
    def _snapshot(self, content):
        temp = tempfile.TemporaryDirectory()
        path = Path(temp.name) / "jobs.log"
        path.write_text(content, encoding="utf-8")
        return temp, dispatch._scan_registry_evidence(str(path))

    def test_terminal_registered_stage_is_indexed_and_route_done_survives(self):
        temp, (routes, terminal) = self._snapshot(
            registry_row("done", "att-terminal", parent="owner",
                         parent_attempt_id="att-owner")
        )
        self.addCleanup(temp.cleanup)
        self.assertEqual(terminal["att-terminal"]["route_node"], "one-shot")
        self.assertEqual(routes["rt-f40"]["one-shot"]["status"], "done")
        self.assertEqual(
            routes["rt-f40"]["one-shot"]["_registry_path"],
            str(Path(temp.name) / "jobs.log"),
        )
        self.assertEqual(routes["rt-f40"]["one-shot"]["parent_attempt_id"],
                         "att-owner")
        self.assertEqual(routes["rt-f40"]["one-shot"]["registry_order"], 0)

    def test_latest_row_wins_for_the_same_exact_attempt(self):
        temp, (_routes, terminal) = self._snapshot(
            registry_row("done", "att-reopened")
            + registry_row("running", "att-reopened")
        )
        self.addCleanup(temp.cleanup)
        self.assertNotIn("att-reopened", terminal)

    def test_retry_attempts_are_independent_even_with_one_slug_and_node(self):
        temp, (routes, terminal) = self._snapshot(
            registry_row("done", "att-old")
            + registry_row("running", "att-new")
        )
        self.addCleanup(temp.cleanup)
        self.assertIn("att-old", terminal)
        self.assertNotIn("att-new", terminal)
        node = routes["rt-f40"]["one-shot"]
        self.assertEqual(node["status"], "running")
        self.assertEqual(
            [item["attempt_id"] for item in node["attempt_history"]],
            ["att-old", "att-new"],
        )

    def test_legacy_terminal_row_never_authorizes_suppression(self):
        legacy = "capability=autopilot-code,depth=2"
        temp, (_routes, terminal) = self._snapshot(
            registry_row("done", "att-legacy", prefix=legacy)
        )
        self.addCleanup(temp.cleanup)
        self.assertNotIn("att-legacy", terminal)


class ExactTerminalSuppressionTest(unittest.TestCase):
    def _terminal(self):
        temp = tempfile.TemporaryDirectory()
        path = Path(temp.name) / "jobs.log"
        path.write_text(registry_row("done", "att-terminal"), encoding="utf-8")
        terminal = dispatch._scan_registry_evidence(str(path))[1]
        self.addCleanup(temp.cleanup)
        return terminal

    def test_only_exact_proc_attempt_and_route_tuple_is_suppressed(self):
        terminal = self._terminal()
        exact = proc_job("att-terminal")
        same_cwd_other_attempt = proc_job("att-unrelated")
        same_attempt_other_node = proc_job("att-terminal", route_node="test")
        registry_source = proc_job("att-terminal", source="jobs")
        kept = dispatch._suppress_terminal_attempt_proc_jobs(
            [exact, same_cwd_other_attempt, same_attempt_other_node, registry_source],
            terminal,
        )
        self.assertEqual(
            kept,
            [same_cwd_other_attempt, same_attempt_other_node, registry_source],
        )

    def test_session_with_same_attempt_is_outside_proc_job_suppression(self):
        terminal = self._terminal()
        native = Session(
            harness="codex", pid=777, cwd="/work/shared",
            attempt_id="att-terminal", route_id="rt-f40", route_node="one-shot",
            subagents=[],
        )
        kept = dispatch._suppress_terminal_attempt_proc_jobs([native], terminal)
        self.assertEqual(kept, [native])

    def test_collect_keeps_terminal_route_stage_and_drops_drain_duplicate(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "jobs.log"
            path.write_text(registry_row("done", "att-terminal"), encoding="utf-8")
            draining = proc_job("att-terminal")
            with mock.patch.object(dispatch, "_scan_processes", return_value=[draining]):
                jobs = dispatch.collect(jobs_path=str(path))
        self.assertEqual(jobs, [])
        self.assertEqual(
            dispatch.collect.last_route_nodes["rt-f40"]["one-shot"]["status"],
            "done",
        )
        self.assertIn("att-terminal", dispatch.collect.last_terminal_attempts)


if __name__ == "__main__":
    unittest.main()
