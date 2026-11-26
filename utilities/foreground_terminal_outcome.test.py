#!/usr/bin/env python3
"""실제 OS 종료 × terminal envelope: 모델 없이 foreground 소비 경계를 실행한다."""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import ast
import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "utilities"))


def foreground_cell(harness):
    path = ROOT / f"adapters/{harness}/bin/dispatch-headless.py"
    spec = importlib.util.spec_from_file_location(f"{harness}_foreground_fixture", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # Execute the production consumer, not a mirrored classifier. Only the
    # preceding model/auth launch and subsequent delivery transport are omitted.
    # The consumer is the launch the three wrappers share; it reads each
    # wrapper's names through `h`.
    shared = ROOT / "utilities" / "dispatch_headless_main.py"
    cells = [node for node in ast.walk(ast.parse(shared.read_text()))
             if isinstance(node, ast.If)
             and ast.unparse(node.test) == "args.launch_lifecycle == h.FOREGROUND_SCOPED"
             and any(isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
                     and call.func.attr == "wait_foreground" for call in ast.walk(node))]
    if len(cells) != 1:
        raise AssertionError(f"foreground consumer changed: {shared}")
    if "DISPATCH_MAIN.launch_claimed_attempt(" not in path.read_text():
        raise AssertionError(f"foreground consumer changed: {path}")
    return module, compile(ast.Module(body=cells[0].body, type_ignores=[]), str(shared), "exec")


def foreground_spec(harness, module, args, jobs):
    """The settlement hook each wrapper's LaunchSpec passes."""
    settle = None
    if harness == "codex":
        def settle(outcome, terminal, terminal_closed):
            module.complete_foreground_wrapper_pass(args, jobs, outcome, terminal, terminal_closed)
    return SimpleNamespace(harness=harness, after_foreground_settle=settle)


class ForegroundOutcomeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cells = {name: foreground_cell(name) for name in ("codex", "claude", "opencode")}

    def run_cell(self, harness, ending, verdict, *, already_closed=False):
        with tempfile.TemporaryDirectory(prefix="foreground-outcome-") as td:
            base = Path(td)
            repo = base / "repo"
            repo.mkdir()
            artifacts = base / "artifacts"
            artifacts.mkdir()
            report = artifacts / "review.md"
            report.write_text("독립 회귀용 보고서\n")
            log_path = base / "worker.jsonl"
            text = (f"artifact: {report}\nverdict: {verdict}\nblocker: "
                    + ("none" if verdict == "PASS" else "fixture"))
            if verdict == "invalid":
                text = "damaged terminal envelope"
            if harness == "claude":
                rows = [{"type": "result", "subtype": "success", "is_error": False, "result": text}]
            else:
                rows = [{"type": "item.completed", "item": {"type": "agent_message", "text": text}},
                        {"type": "turn.completed"}]
            jobs = base / "jobs.log"
            attempt = "att-foreground-fixture"
            metadata = ("attempt_schema_version=2,dispatch_depth=2,transport=headless,"
                        "execution_surface=registered-headless,registered_worker=1,"
                        "fallback_hop=same-harness-headless,worker_type=review,"
                        f"harness={harness},attempt_id={attempt},launch_started=1,log_file={log_path}")
            if already_closed:
                metadata += ",note=completed-marker,failure_class=pass"
            jobs.write_text(f"2026-09-08T00:00:00Z\t{'done' if already_closed else 'open'}\t"
                            f"{repo}\t{repo}\tfixture\t{metadata}\n")
            args = SimpleNamespace(parent_binding=None, foreground_timeout=.2 if ending == "timeout" else 2,
                                   attempt_id=attempt, worker_type="review", worktree=repo,
                                   artifact_root=artifacts, slug="fixture", registered_worker=True,
                                   route_file=None, route_node=None)
            module, cell = self.cells[harness]
            env = {"PATH": os.environ.get("PATH", ""), "HOME": str(base),
                   "AGENT_HOME": str(ROOT), "AGENT_DISPATCH_JOBS": str(jobs),
                   "AGENT_ARTIFACT_ROOT": str(artifacts), "XDG_STATE_HOME": str(base / "state"),
                   "PYTHONDONTWRITEBYTECODE": "1"}
            parent = None
            timer = None
            observed = []
            # Every case publishes its final envelope before the requested OS
            # exit. READY keeps PID/start capture separate from launch timing.
            child = ("import sys; from pathlib import Path; "
                     "Path(sys.argv[1]).write_text(sys.argv[2]); "
                     "print('READY',flush=True); sys.stdin.readline(); sys.exit(int(sys.argv[3]))")
            with mock.patch.dict(os.environ, env, clear=True):
                proc = subprocess.Popen([harness, "-c", child, str(log_path),
                                         "\n".join(map(json.dumps, rows)) + "\n",
                                         "7" if ending == "exit7" else "0"],
                                        executable=sys.executable, stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, start_new_session=True)
                try:
                    self.assertEqual(proc.stdout.readline(), b"READY\n")
                    if ending == "parent-exit":
                        parent = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.read()"],
                                                  stdin=subprocess.PIPE)
                        timer = threading.Timer(.05, parent.stdin.close)
                    elif ending == "signal":
                        timer = threading.Timer(.05, lambda: proc.send_signal(signal.SIGTERM))
                    elif ending not in {"timeout", "no-progress"}:
                        def finish():
                            proc.stdin.write(b"go\n")
                            proc.stdin.flush()
                        timer = threading.Timer(.05, finish)

                    def wait(*pos, **kw):
                        if parent is not None:
                            kw["parent_is_live"] = lambda: parent.poll() is None
                        result = module.wait_foreground(*pos, **kw)
                        observed.append(result)
                        return result

                    args.watchdog_budget = module.begin_finite_watchdog(args.foreground_timeout)
                    adapter = dict(vars(module), wait_foreground=wait)
                    namespace = dict(h=module.DISPATCH_MAIN.AdapterNamespace(adapter), args=args,
                                     proc=proc, jobs=jobs, log_path=log_path,
                                     spec=foreground_spec(harness, module, args, jobs),
                                     activity_context=None)
                    if ending == "no-progress":
                        from foreground_activity import ForegroundActivityContext
                        namespace["activity_context"] = ForegroundActivityContext(
                            0.12, harness, log_path, {}, runtime_executable="/bin/false"
                        )
                    with mock.patch.object(module, "materialize_after_terminal_close") as delivery:
                        adapter["materialize_after_terminal_close"] = delivery
                        if timer:
                            timer.start()
                        exec(cell, namespace)
                finally:
                    if timer:
                        timer.join()
                    if proc.poll() is None:
                        proc.kill()
                    proc.wait()
                    proc.stdin.close()
                    proc.stdout.close()
                    if parent is not None:
                        parent.stdin.close()
                        if parent.poll() is None:
                            parent.kill()
                        parent.wait()
            self.assertEqual(len(observed), 1)
            outcome = observed[0]
            expected = {"exit0": "", "exit7": "exit-7", "signal": "signal-15",
                        "timeout": "timeout", "parent-exit": "parent-terminated",
                        "no-progress": "no-progress"}[ending]
            self.assertEqual(outcome.failure, expected)
            if ending in {"exit0", "exit7", "signal"}:
                self.assertEqual(outcome.exit_code, {"exit0": 0, "exit7": 7, "signal": -15}[ending])
            self.assertTrue(outcome.group_empty)
            fields = jobs.read_text().strip().split("\t")
            meta = dict(item.split("=", 1) for item in fields[5].split(",") if "=" in item)
            result = {"adapter": harness, "ending": ending, "envelope": verdict,
                      "actual_exit": outcome.exit_code, "actual_failure": outcome.failure,
                      "group_empty": outcome.group_empty, "worker_exit": args.worker_exit,
                      "worker_failure": args.worker_failure, "status": fields[1], "metadata": meta,
                      "terminal_verdict": getattr(args, "terminal_verdict", None)}
            print(json.dumps(result, ensure_ascii=False), flush=True)
            return result

    def test_actual_failure_dominates_every_envelope_across_adapters(self):
        for harness in self.cells:
            for ending in ("exit7", "signal", "timeout", "parent-exit", "no-progress"):
                for verdict in ("PASS", "FAIL", "BLOCKED", "invalid"):
                    with self.subTest(adapter=harness, ending=ending, envelope=verdict):
                        r = self.run_cell(harness, ending, verdict)
                        self.assertEqual(r["worker_exit"], r["actual_exit"])
                        self.assertEqual(r["worker_failure"], r["actual_failure"])
                        self.assertEqual(r["metadata"]["note"], "dead-" + r["actual_failure"])
                        self.assertNotEqual(r["metadata"].get("failure_class"), "pass")
                        if harness != "opencode":
                            self.assertEqual(r["metadata"].get("process_exit"), str(r["actual_exit"]))
                            self.assertEqual(r["metadata"].get("reconcile_reason"), r["actual_failure"])
                            self.assertEqual(r["terminal_verdict"], None if verdict == "invalid" else verdict)

    def test_zero_exit_preserves_semantic_review_and_invalid_envelope_boundary(self):
        for harness in ("codex", "claude"):
            for verdict in ("PASS", "FAIL", "BLOCKED", "invalid"):
                with self.subTest(adapter=harness, envelope=verdict):
                    r = self.run_cell(harness, "exit0", verdict)
                    self.assertEqual(r["worker_exit"], 0)
                    if verdict in {"PASS", "invalid"}:
                        self.assertEqual(r["status"], "open")
                        self.assertEqual(r["worker_failure"], "")
                        self.assertNotIn("note", r["metadata"])
                    else:
                        note = "completed-review-blocking" if verdict == "FAIL" else "dead-worker-blocked"
                        self.assertEqual(r["metadata"]["note"], note)
                        self.assertEqual(r["metadata"].get("process_exit"), "0")
                        if verdict == "FAIL":
                            self.assertIn("review_artifact_b64", r["metadata"])

    def test_closed_marker_is_immutable_but_start_receipt_keeps_actual_failure(self):
        for harness in ("codex", "claude"):
            with self.subTest(adapter=harness):
                r = self.run_cell(harness, "exit7", "PASS", already_closed=True)
                self.assertEqual(r["metadata"]["note"], "completed-marker")
                self.assertEqual(r["metadata"]["failure_class"], "pass")
                self.assertEqual(r["worker_exit"], 7)
                self.assertEqual(r["worker_failure"], "exit-7")
                self.assertEqual(r["terminal_verdict"], "PASS")


if __name__ == "__main__":
    unittest.main()
