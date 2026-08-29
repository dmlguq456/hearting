#!/usr/bin/env python3
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
import warnings
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "utilities"), str(ROOT / "tools")]

import dispatch_summary as S  # noqa: E402
from fleet import titles  # noqa: E402

warnings.filterwarnings("ignore", category=ResourceWarning)


class DispatchSummaryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_env = os.environ.copy()
        os.environ.update({
            "FLEET_TITLE_STATE_DIR": str(Path(self.tmp.name) / "titles"),
            "AGENT_MODEL_GOVERNOR_ROOT": str(Path(self.tmp.name) / "governor"),
            "AGENT_MODEL_WORKER_TOTAL": "5",
            "AGENT_MODEL_WORKER_START_BUDGET": "20",
            "FLEET_TITLE_CONCURRENCY": "4",
            "FLEET_TITLE_MAX_STARTS": "4",
            "FLEET_TITLE_COMMAND": shlex.join([
                sys.executable, "-c",
                "print('TITLE: Dispatch Summary Owner\\nNOW: 분사 작업 요약을 갱신하고 있습니다')",
            ]),
        })
        os.environ.pop("FLEET_TITLE_DISABLE", None)
        os.environ.pop("FLEET_TITLE_REFRESH", None)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.old_env)
        self.tmp.cleanup()

    def test_prompt_validator_accepts_exact_sibling_and_rejects_neighbor(self):
        attempt = "att-prompt-validator"
        directory = Path(self.tmp.name)
        log = directory / f"owner.{attempt}.codex.jsonl"
        prompt = directory / f"owner.{attempt}.codex.prompt.txt"
        neighbor = directory / "owner.other.codex.prompt.txt"
        log.write_text("{}\n", encoding="utf-8")
        prompt.write_text("Assignment:\nanchor\n", encoding="utf-8")
        neighbor.write_text("neighbor\n", encoding="utf-8")
        self.assertEqual(S._validate_prompt(prompt, S._validate_transcript(log, "codex", attempt)), prompt)
        with self.assertRaises(ValueError):
            S._validate_prompt(neighbor, S._validate_transcript(log, "codex", attempt))

    def test_supervisor_generates_initial_and_final_sidecar_without_fleet(self):
        attempt = "att-summary-live"
        log = Path(self.tmp.name) / f"owner.{attempt}.codex.jsonl"
        log.write_text(json.dumps({
            "type": "item.completed",
            "item": {"type": "agent_message", "text": "start summary owner"},
        }) + "\n", encoding="utf-8")
        child = subprocess.Popen([
            sys.executable, "-c",
            "import pathlib,sys,time,json; time.sleep(.4); "
            "p=pathlib.Path(sys.argv[1]); "
            "f=p.open('a'); f.write(json.dumps({'type':'item.completed','item':"
            "{'type':'agent_message','text':'finish summary owner'}})+'\\n'); "
            "f.close(); time.sleep(.25)",
            str(log),
        ], start_new_session=True)
        start = S.process_observation(child.pid)[1]
        self.assertTrue(start)
        rc = S.supervise(
            attempt_id=attempt, harness="codex", transcript=log,
            target_pid=child.pid, target_start=start,
            poll=0.05, initial_delay=0, periodic_debounce=90,
            final_grace=8, log_quiet=0.05,
        )
        child.wait(timeout=5)
        self.assertEqual(rc, 0)
        sidecar = titles.read(S.summary_sid(attempt), harness="codex")
        self.assertEqual(sidecar["title"], "Dispatch Summary Owner")
        self.assertEqual(sidecar["summary"], "분사 작업 요약을 갱신하고 있습니다")
        self.assertEqual(sidecar["offset"], log.stat().st_size)
        state = json.loads(S.owner_state_path("codex", attempt).read_text())
        self.assertEqual(state["status"], "terminal")
        self.assertTrue(state["final_refresh_complete"])

    def test_reconcile_reattaches_only_one_live_exact_attempt(self):
        attempt = "att-summary-recover"
        worker = subprocess.Popen(["sleep", "60"], start_new_session=True)
        owner = subprocess.Popen(["sleep", "60"], start_new_session=True)
        try:
            start = S.process_observation(worker.pid)[1]
            log = Path(self.tmp.name) / f"owner.{attempt}.claude.jsonl"
            log.write_text('{"message":"recover summary"}\n', encoding="utf-8")
            prompt = Path(self.tmp.name) / f"owner.{attempt}.claude.prompt.txt"
            prompt.write_text("Assignment:\nRetained exact anchor\n", encoding="utf-8")
            jobs = Path(self.tmp.name) / "jobs.log"
            jobs.write_text(
                "2026-08-04T00:00:00Z\topen\t/repo\t/wt\towner\t"
                "attempt_schema_version=2,dispatch_depth=1,transport=headless,"
                "execution_surface=registered-headless,registered_worker=1,"
                "fallback_hop=same-harness-headless,worker_type=owner,"
                f"attempt_id={attempt},harness=claude,pid={worker.pid},"
                f"pid_start={start},log_file={log}\n",
                encoding="utf-8",
            )
            owner_start = S.process_observation(owner.pid)[1]
            observer_namespace = S.process_namespace_identity()
            attached = {
                "summary_owner": S.OWNER_KIND,
                "summary_sid": S.summary_sid(attempt),
                "summary_owner_pid": str(owner.pid),
                "summary_owner_pid_start": owner_start,
                "summary_owner_pid_observer_ns": observer_namespace,
                "summary_state_file": str(S.owner_state_path("claude", attempt)),
            }
            state_path = S.owner_state_path("claude", attempt)
            state_path.parent.mkdir(parents=True, exist_ok=True)
            state_path.write_text(json.dumps({
                "schema_version": S.OWNER_SCHEMA,
                "status": "active",
                "attempt_id": attempt,
                "harness": "claude",
                "pid": owner.pid,
                "proc_start": owner_start,
                "observer_namespace": observer_namespace,
                "prompt_path": str(prompt),
            }))
            with mock.patch.object(S, "launch_summary_owner", return_value=attached) as launch:
                first = S.ensure_attempt_owner(jobs, attempt)
                second = S.ensure_attempt_owner(jobs, attempt)
            self.assertEqual(first["state"], "started")
            self.assertEqual(second, {"state": "existing", "reason": "owner-live"})
            launch.assert_called_once()
            self.assertEqual(launch.call_args.kwargs["prompt_path"], prompt)
            self.assertIn("summary_owner=dispatch-v1", jobs.read_text())
        finally:
            for process in (worker, owner):
                if process.poll() is None:
                    process.kill()
                process.wait()

    def test_reconcile_invalid_retained_prompt_degrades_without_globbing(self):
        worker = subprocess.Popen(["sleep", "60"], start_new_session=True)
        try:
            start = S.process_observation(worker.pid)[1]
            observer_namespace = S.process_namespace_identity()
            invalid_paths = {
                "missing": Path(self.tmp.name) / "owner.att-missing.claude.prompt.txt",
                "moved": Path(self.tmp.name) / "moved.prompt.txt",
                "symlink": Path(self.tmp.name) / "owner.att-symlink.claude.prompt.txt",
                "foreign": Path(self.tmp.name) / "owner.att-foreign.codex.prompt.txt",
                "suffix": Path(self.tmp.name) / "owner.att-suffix.claude.txt",
            }
            invalid_paths["moved"].write_text("moved\n", encoding="utf-8")
            foreign_target = invalid_paths["foreign"]
            foreign_target.write_text("foreign\n", encoding="utf-8")
            invalid_paths["symlink"].symlink_to(foreign_target)
            for label, retained in invalid_paths.items():
                attempt = "att-invalid-" + label
                log = Path(self.tmp.name) / f"owner.{attempt}.claude.jsonl"
                log.write_text('{"message":"recover summary"}\n', encoding="utf-8")
                jobs = Path(self.tmp.name) / (label + ".jobs.log")
                jobs.write_text(
                    "2026-08-04T00:00:00Z\topen\t/repo\t/wt\towner\t"
                    "attempt_schema_version=2,dispatch_depth=1,transport=headless,"
                    "execution_surface=registered-headless,registered_worker=1,"
                    "fallback_hop=same-harness-headless,worker_type=owner,"
                    f"attempt_id={attempt},harness=claude,pid={worker.pid},"
                    f"pid_start={start},log_file={log}\n",
                    encoding="utf-8",
                )
                state_path = S.owner_state_path("claude", attempt)
                state_path.parent.mkdir(parents=True, exist_ok=True)
                state_path.write_text(json.dumps({
                    "schema_version": S.OWNER_SCHEMA,
                    "status": "terminal",
                    "attempt_id": attempt,
                    "harness": "claude",
                    "pid": 0,
                    "proc_start": "not-live",
                    "observer_namespace": observer_namespace,
                    "prompt_path": str(retained),
                }))
                attached = {"summary_owner": S.OWNER_KIND, "summary_sid": S.summary_sid(attempt)}
                with mock.patch.object(S, "launch_summary_owner", return_value=attached) as launch:
                    result = S.ensure_attempt_owner(jobs, attempt)
                self.assertEqual(result["state"], "started", label)
                self.assertIsNone(launch.call_args.kwargs["prompt_path"], label)
        finally:
            if worker.poll() is None:
                worker.kill()
            worker.wait()

    def test_launch_summary_owner_passes_one_prompt_pair_without_contents(self):
        attempt = "att-summary-argv"
        worker = subprocess.Popen(["sleep", "60"], start_new_session=True)
        try:
            start = S.process_observation(worker.pid)[1]
            log = Path(self.tmp.name) / f"owner.{attempt}.codex.jsonl"
            prompt = Path(self.tmp.name) / f"owner.{attempt}.codex.prompt.txt"
            log.write_text("{}\n", encoding="utf-8")
            prompt.write_text("Assignment:\nsecret anchor contents\n", encoding="utf-8")
            captured = {}

            class FakeOwner:
                pid = 54321

                def poll(self):
                    return None

            def fake_popen(argv, **kwargs):
                captured["argv"] = list(argv)
                captured["env"] = dict(kwargs["env"])
                return FakeOwner()

            def fake_identity(_pid):
                state_path = S.owner_state_path("codex", attempt)
                state_path.write_text(json.dumps({
                    "schema_version": S.OWNER_SCHEMA,
                    "status": "active",
                    "attempt_id": attempt,
                    "harness": "codex",
                    "pid": 54321,
                    "proc_start": "owner-start",
                    "observer_namespace": S.process_namespace_identity(),
                }))
                return {"pid_start": "owner-start"}

            with mock.patch.object(S.subprocess, "Popen", side_effect=fake_popen), \
                    mock.patch.object(S, "process_launch_identity", side_effect=fake_identity):
                S.launch_summary_owner(
                    attempt_id=attempt, harness="codex", transcript=log,
                    target_pid=worker.pid, target_start=start, prompt_path=prompt,
                )
            self.assertEqual(captured["argv"].count("--prompt"), 1)
            prompt_index = captured["argv"].index("--prompt")
            self.assertEqual(captured["argv"][prompt_index + 1], str(prompt))
            self.assertNotIn("secret anchor contents", " ".join(captured["argv"]))
            self.assertNotIn("secret anchor contents", " ".join(captured["env"].values()))
        finally:
            if worker.poll() is None:
                worker.kill()
            worker.wait()

    def _announce(self, session_id: str, cwd: str) -> str:
        return json.dumps({
            "type": "dispatch.supervisor.session",
            "parent_attempt_id": "att-x",
            "session_id": session_id,
            "cwd": cwd,
        }) + "\n"

    def test_supervised_receipt_log_is_never_its_own_summary_source(self):
        """A supervised owner's receipt log carries no model text.

        Regression: refreshing against it wrote an empty title with no NOW line and
        advanced the cursor past the whole run, so the sub-session stayed nameless
        for its entire lifetime.
        """
        log = Path(self.tmp.name) / "owner.att-sup.claude.jsonl"
        log.write_text(
            self._announce("11111111-2222-3333-4444-555555555555", "/wt")
            + json.dumps({"type": "result", "subtype": "success", "result": "PASS"}) + "\n",
            encoding="utf-8",
        )
        decided, announced = S.announced_session(log)
        self.assertTrue(decided)
        self.assertEqual(announced["session_id"], "11111111-2222-3333-4444-555555555555")
        # No transcript on disk yet -> wait, never fall back to the receipt log.
        self.assertIsNone(S._summary_source(log, {}))

    def test_one_shot_log_stays_its_own_summary_source(self):
        log = Path(self.tmp.name) / "owner.att-oneshot.claude.jsonl"
        log.write_text(
            json.dumps({"type": "assistant", "message": {"content": [
                {"type": "text", "text": "streamed turn"}]}}) + "\n",
            encoding="utf-8",
        )
        decided, announced = S.announced_session(log)
        self.assertTrue(decided)
        self.assertIsNone(announced)
        self.assertEqual(S._summary_source(log, {}), log)

    def test_empty_log_is_undecided_and_never_cached_as_unsupervised(self):
        """The summary owner starts before the worker clears its launch fence."""
        log = Path(self.tmp.name) / "owner.att-race.claude.jsonl"
        log.write_text("", encoding="utf-8")
        self.assertEqual(S.announced_session(log), (False, None))
        cache: dict = {}
        self.assertIsNone(S._summary_source(log, cache))
        self.assertNotIn("source", cache)
        self.assertNotIn("announced", cache)
        # The announcement lands on a later poll and must still be honoured.
        session_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        projects = Path(self.tmp.name) / "config" / "projects" / "-wt"
        projects.mkdir(parents=True)
        (projects / f"{session_id}.jsonl").write_text("{}\n", encoding="utf-8")
        log.write_text(self._announce(session_id, "/wt"), encoding="utf-8")
        os.environ["CLAUDE_CONFIG_DIR"] = str(Path(self.tmp.name) / "config")
        self.assertEqual(
            S._summary_source(log, cache), projects / f"{session_id}.jsonl")

    def test_announced_transcript_resolves_by_exact_session_id(self):
        session_id = "99999999-8888-7777-6666-555555555555"
        config = Path(self.tmp.name) / "config"
        os.environ["CLAUDE_CONFIG_DIR"] = str(config)
        # Encoded-cwd hit.
        encoded = config / "projects" / "-home-user-cairn-wt-hook-sweep"
        encoded.mkdir(parents=True)
        exact = encoded / f"{session_id}.jsonl"
        exact.write_text("{}\n", encoding="utf-8")
        self.assertEqual(
            S.session_transcript(
                {"session_id": session_id, "cwd": "/home/user/agent_note.wt/hook_sweep"}),
            exact,
        )
        # A cwd that does not encode to the runtime's project dir still resolves by id.
        self.assertEqual(
            S.session_transcript({"session_id": session_id, "cwd": "/somewhere/else"}),
            exact,
        )
        # A missing transcript never borrows a same-dir neighbour.
        self.assertIsNone(
            S.session_transcript({"session_id": "00000000-0000-0000-0000-000000000000",
                                  "cwd": "/home/user/agent_note.wt/hook_sweep"}))

    def test_supervised_owner_gets_a_sidecar_from_the_announced_transcript(self):
        """End-to-end: announcement -> child transcript -> title and NOW line.

        This is the path that produced no summary at all: the receipt log alone
        carries no model text, so the sidecar has to come from the child's own
        transcript for a supervised owner to be nameable in Fleet.
        """
        attempt = "att-supervised-e2e"
        session_id = "abcdabcd-1234-5678-9abc-abcdabcdabcd"
        config = Path(self.tmp.name) / "config"
        os.environ["CLAUDE_CONFIG_DIR"] = str(config)
        project = config / "projects" / "-wt-owner"
        project.mkdir(parents=True)
        transcript = project / f"{session_id}.jsonl"
        transcript.write_text(json.dumps({
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "owner turn one"}]},
        }) + "\n", encoding="utf-8")
        log = Path(self.tmp.name) / f"owner.{attempt}.claude.jsonl"
        log.write_text(self._announce(session_id, "/wt/owner"), encoding="utf-8")
        child = subprocess.Popen([
            sys.executable, "-c",
            "import pathlib,sys,time,json; time.sleep(.4); "
            "p=pathlib.Path(sys.argv[1]); f=p.open('a'); "
            "f.write(json.dumps({'type':'assistant','message':{'content':"
            "[{'type':'text','text':'owner turn two'}]}})+'\\n'); f.close(); time.sleep(.25)",
            str(transcript),
        ], start_new_session=True)
        start = S.process_observation(child.pid)[1]
        self.assertTrue(start)
        rc = S.supervise(
            attempt_id=attempt, harness="claude", transcript=log,
            target_pid=child.pid, target_start=start,
            poll=0.05, initial_delay=0, periodic_debounce=90,
            final_grace=8, log_quiet=0.05,
        )
        child.wait(timeout=5)
        self.assertEqual(rc, 0)
        sidecar = titles.read(S.summary_sid(attempt), harness="claude")
        self.assertEqual(sidecar["title"], "Dispatch Summary Owner")
        self.assertEqual(sidecar["summary"], "분사 작업 요약을 갱신하고 있습니다")
        # The cursor tracks the transcript, never the receipt log.
        self.assertEqual(sidecar["offset"], transcript.stat().st_size)
        self.assertNotEqual(sidecar["offset"], log.stat().st_size)
        state = json.loads(S.owner_state_path("claude", attempt).read_text())
        self.assertEqual(state["summary_source"], str(transcript))
        self.assertEqual(state["status"], "terminal")

    def test_supervisor_announces_its_child_session_before_the_first_turn(self):
        source = (ROOT / "utilities" / "claude-session-supervisor.py").read_text()
        announce = source.index('"type": "dispatch.supervisor.session"')
        first_turn = source.index("result, process_rc = run_turn(")
        self.assertLess(announce, first_turn)

    def test_all_three_wrappers_attach_owner_at_pre_release_boundary(self):
        for harness in ("claude", "codex", "opencode"):
            source = (ROOT / "adapters" / harness / "bin" / "dispatch-headless.py").read_text()
            self.assertIn("from dispatch_summary import launch_summary_owner", source)
            self.assertIn("pre_release=lambda identity: launch_summary_owner", source)
            self.assertIn(f'harness="{harness}"', source)
        opencode = (ROOT / "adapters" / "opencode" / "bin" / "dispatch-headless.py").read_text()
        self.assertIn("args.command_attempt_id = args.attempt_id", opencode)


if __name__ == "__main__":
    unittest.main(verbosity=2)
