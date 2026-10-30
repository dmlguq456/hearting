#!/usr/bin/env python3
"""One `start` resumes an owner that stopped at a usage limit (no model calls).

`work_start.start_work` and the real `dispatch_replacement.advance/claim/admission`
run end to end. Only the route file, the reuse snapshot and process probes are
replaced, and the launcher is a function that registers the replacement row the way
the adapter does (through `replacement_row`, so admission is the production one).
"""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
from types import SimpleNamespace

import dispatch_contract as D
import dispatch_replacement as R
import work_start as W
from dispatch_completion_join import CurrentDeliveryState
os.environ.setdefault("HEARTING_CAPACITY_PROBE_CLI", os.devnull)  # never ask a real Claude login

LIMITED = "resets 2099-01-01 00:00 (UTC)"
FREE = "resets 2020-01-01 00:00 (UTC)"


class CapacityResumeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.jobs = self.root / "jobs.log"
        self.jobs.touch()
        self.route = {"route_id": "rt-cap", "route_hash": "sha256:cap", "slug": "task",
                      "artifact_root": str(self.root / "artifacts"), "cwd": str(self.root),
                      "capability": "autopilot-code", "effective_intensity": "standard", "owner_model_profile": "deep",
                      "work_request": {"text": "the raw task", "owner_harness": "claude"}, "nodes": []}
        self.path = self.root / "route.json"
        self.path.write_text(json.dumps(self.route))
        self.calls = []
        self.launch_error = None
        self.ready = False
        self.statuses = {}
        self.launch_home = None
        for patcher in (
                mock.patch.object(W, "default_parent_session_id", return_value="parent"),
                mock.patch.dict(os.environ, {"AGENT_CODEX_MANAGED_GATEWAY": "0", "HEARTING_GATES": "off"}),
                mock.patch.object(W, "join_selected_attempts", side_effect=self.observe),
                mock.patch.object(W, "current_delivery_state", side_effect=self.delivery),
                mock.patch.object(R, "_route", return_value=(self.path, self.route)),
                mock.patch.object(R, "_logical_key", side_effect=lambda r, m: {
                    "root_route_id": "rt-root", "node": "__owner__" if m.get("worker_type") == "owner" else m["route_node"]}),
                mock.patch.object(R, "_reuse_snapshot", return_value={
                    "completed": [], "cycle_id": "cyc", "producer_id": "prod", "gates": []}),
                mock.patch.object(D, "attempt_process_quiescence", side_effect=self.quiescence),
                mock.patch.object(D, "resolve_attempt_cleanup", side_effect=self.cleanup),
                mock.patch("artifact_producer.prepare_route_artifact_env", return_value={})):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.quiet = {}
        self.cleaned = []
        self.cleanup_settles = True
        self.owner = self.seed_owner("att-owner", note="dead-capacity", failure_class="capacity",
                                     result=LIMITED)

    # -- fixtures ------------------------------------------------------------------
    def observe(self, **kw):
        return {"state": "ready" if self.ready else "timeout", "children": []}

    def delivery(self, jobs, aid, **kw):
        fields = dict(marker={"artifact": "/exact/report.md"}, marker_digest="sha256:marker",
                      row_revision="1", row_digest="sha256:row", status="done", verdict="PASS",
                      quiescent=True, owned_children=0, advanced=False, completion_proven=True)
        fields.update(self.statuses.get(aid, {}))
        return CurrentDeliveryState(**fields)

    def quiescence(self, meta, terminal_receipt=False):
        row = self.rows().get(meta.get("attempt_id"))
        # The mocked launcher registers a running process without spawning it.
        default = ("live", "mock-owner-running", 12) if (
            row and row[0][1] in {"open", "running"} and meta.get("launch_claimed") == "1"
        ) else ("quiescent", "process-absent", None)
        state = self.quiet.get(meta.get("attempt_id"), default)
        return SimpleNamespace(state=state[0], reason=state[1], pid=state[2])

    def cleanup(self, jobs, aid, apply=False):
        self.cleaned.append((aid, apply))
        if self.cleanup_settles:
            self.quiet[aid] = ("quiescent", "cleanup-proven", None)
        return {"attempt_id": aid, "settled": self.cleanup_settles}

    def write(self, meta, status="done", stamp=None, append=True):
        stamp = stamp or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        line = (stamp + "\t" + status + "\t" + str(self.root) + "\t" + str(self.root) + "\ttask\t"
                + ",".join(k + "=" + v for k, v in meta.items()) + "\n")
        with self.jobs.open("a" if append else "w") as f:
            f.write(line)

    def rows(self):
        return R._rows(self.jobs.read_text().splitlines())

    def set_status(self, aid, status, **changes):
        lines = []
        for line in self.jobs.read_text().splitlines():
            fields = line.split("\t")
            if len(fields) == 6 and D.row_has_attempt(fields[5], aid):
                fields[1] = status
                if changes:
                    meta = {**D.parse_registry_metadata(fields[5]), **changes}
                    fields[5] = ",".join(k + "=" + v for k, v in meta.items())
                line = "\t".join(fields)
            lines.append(line)
        self.jobs.write_text("\n".join(lines) + "\n")

    def seed_owner(self, aid, *, note, failure_class, result=None, harness="claude", **extra):
        log = self.root / (aid + ".log")
        log.write_text("")  # a silent death left no result to settle
        if result is not None:
            log.write_text(json.dumps({"type": "result", "is_error": True, "api_error_status": 429,
                "session_id": "s", "result": "You've hit your session limit · " + result}) + "\n")
        meta = {"attempt_schema_version": "2", "dispatch_depth": "1", "transport": "headless",
                "execution_surface": "registered-headless", "registered_worker": "1",
                "fallback_hop": "same-harness-headless", "attempt_id": aid, "route_id": "rt-cap",
                "route_hash": "sha256:cap", "route_node": "owner", "worker_type": "owner",
                "parent_sid": "parent", "harness": harness, "model_profile": "deep", "note": note, "failure_class": failure_class,
                "launch_outcome": "never-launched", "log_file": str(log), "artifact_root": str(self.root / "artifacts"),
                "launch_started": "1", "parent_completion_delivery": "codex-managed-gateway", **extra}
        args = SimpleNamespace(attempt_id=aid, jobs_path=self.jobs, worktree=str(self.root),
                               route_id="rt-cap", route_node="owner", model_profile="deep",
                               replacement_input_argv=["--start", "--attempt-id", aid, "--prompt-text", "the raw task"])
        if harness == "opencode":
            args.opencode_config_content = json.dumps({
                "permissions": [{"action": "read", "resource": "*", "effect": "allow"}],
                "agents": {},
            })
        meta.update(D.parse_registry_metadata(R.seal_launch_input(args, harness, "the raw task")))
        self.write(meta, stamp=datetime.fromtimestamp(time.time() - 3600, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
        return meta

    def reseed(self, **kw):
        """Replace the default capacity owner with another terminal owner."""
        import shutil
        self.jobs.write_text("")
        self.quiet, self.cleaned = {}, []
        shutil.rmtree(R._directory(self.jobs), ignore_errors=True)
        self.owner = self.seed_owner("att-owner", **kw)

    def seal_under(self, home):
        """Re-seal the source owner's input as an older installed release."""
        (R._directory(self.jobs) / "inputs" / "att-owner.json").unlink()
        with mock.patch.object(R, "ROOT", home):
            args = SimpleNamespace(attempt_id="att-owner", jobs_path=self.jobs, worktree=str(self.root),
                                   route_id="rt-cap", route_node="owner", model="old-model", model_profile="deep",
                                   replacement_input_argv=["--start", "--attempt-id", "att-owner", "--prompt-text", "the raw task"])
            fragment = R.seal_launch_input(args, "claude", "the raw task")
        self.set_status("att-owner", "done", **D.parse_registry_metadata(fragment))

    def launcher(self, command, **kwargs):
        """Register the replacement row like the adapter: real sealing and real admission."""
        self.calls.append(command)
        if self.launch_error:
            return subprocess.CompletedProcess(command, 1, "", self.launch_error)
        argv = command[2:]

        def value(flag):
            return argv[argv.index(flag) + 1]
        aid, prior = value("--attempt-id"), value("--automatic-retry-of")
        source = self.rows()[prior][1]
        task = Path(value("--prompt-file")).read_text()
        args = SimpleNamespace(attempt_id=aid, jobs_path=self.jobs, worktree=value("--worktree"),
                               route_id="rt-cap", route_node="owner", replacement_input_argv=list(argv),
                               **{"model_profile": source["model_profile"], **(self.candidate_args or {})})
        if source.get("harness") == "opencode" and not hasattr(args, "opencode_config_content"):
            args.opencode_config_content = json.dumps({
                "permissions": [{"action": "read", "resource": "*", "effect": "allow"}],
                "agents": {},
            })
        meta = {k: v for k, v in source.items() if k not in {
            "note", "failure_class", "launch_outcome", "replacement_input_digest", "replacement_family_id",
            "replacement_attempt_id", "replacement_claim_digest", "replacement_original_attempt_id",
            "replacement_ordinal", "automatic_retry_of", "log_file", "cleanup_receipt_digest"}}
        meta.update(attempt_id=aid, automatic_retry_of=prior, launch_claimed="1", log_file=str(self.root / (aid + ".log")))
        meta.update(D.parse_registry_metadata(R.seal_launch_input(args, source["harness"], task)))
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        row = (stamp + "\topen\t12\tparent\ttask\t" + ",".join(k + "=" + v for k, v in meta.items()))
        with R._locked(self.jobs) as lines:
            row, _ = R.replacement_row(self.jobs, lines, row)
            with self.jobs.open("a") as f:
                f.write(row + "\n")
        return subprocess.CompletedProcess(command, 0, "registered=1 started=1 child_spawned=1\n", "")

    candidate_args = None

    def start(self, **kw):
        return W.start_work(self.route, self.path, self.jobs, run=self.launcher, **kw)

    def claims(self):
        return sorted((R._directory(self.jobs) / "claims").glob("*.json")) if (R._directory(self.jobs) / "claims").exists() else []

    def set_limit(self, aid, text):
        (self.root / (aid + ".log")).write_text(json.dumps({"type": "result", "is_error": True,
            "api_error_status": 429, "session_id": "s", "result": "You've hit your session limit · " + text}) + "\n")

    def replacement_id(self, original="att-owner"):
        return next(a for a, (_, m) in self.rows().items() if m.get("automatic_retry_of") == original)

    # -- 1: usage limit pauses; one start after it lifts resumes to completion -------
    def test_capacity_dead_owner_waits_during_limit_then_one_start_resumes_to_completion(self):
        # A current explicit pin holds at that harness's real limit. Unpinned
        # replacements use ordinary selection instead of waiting on the old one.
        self.route["selection_pins"] = {"owner": {"harness": "claude"}}
        for _ in range(2):
            result = self.start()
            self.assertEqual(result["state"], "waiting-capacity", result)
            self.assertEqual(result["required_action"], "resume-after-capacity")
            self.assertEqual(result["reason"], "owner-capacity-wait")
            self.assertTrue(result["retry_at"].startswith("2099-01-01"), result)
            self.assertNotIn("parent_next", result)
            self.assertIn("from the session that owns the route", result["next_step"])
        self.assertEqual((self.calls, self.claims()), ([], []))
        self.assertFalse((R._directory(self.jobs) / "by-source").exists())
        self.set_limit("att-owner", FREE)
        result = self.start()
        self.assertEqual(result["state"], "running", result)
        self.assertEqual(len(self.calls), 1)
        replacement = self.replacement_id()
        self.assertEqual(self.rows()[replacement][1]["automatic_retry_of"], "att-owner")
        self.set_status(replacement, "done", note="completed", failure_class="pass", verdict="PASS")
        self.ready = True
        with mock.patch("dispatch_terminal_commit.owner_workflow_gaps", return_value={}):
            result = self.start()
        self.assertEqual(result["state"], "completed", result)
        self.start()
        self.assertEqual(len(self.calls), 1)

    def test_an_unpinned_limited_owner_reaches_the_ordinary_owner_selector(self):
        result = self.start()
        self.assertEqual(result["state"], "running", result)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0][1], str(R.ROOT / "utilities/dispatch-owner.py"))
        self.assertNotIn("--adapter", self.calls[0])

    def test_codex_pin_waits_for_usable_credits_then_launches_one_successor(self):
        if str(R.ROOT / "tools") not in sys.path:
            sys.path.insert(0, str(R.ROOT / "tools"))
        from fleet.collectors import usage_cache
        # This fixture drives start synchronously; do not leave a detached timer.
        with mock.patch.dict(os.environ, {
                "FLEET_USAGE_STATE_DIR": str(self.root / "usage"),
                "HARNESS_CAPACITY_SCORES": "", "CODEX_HOME": str(self.root / "codex")}), \
                mock.patch("capacity_auto_resume.arm", return_value=None):
            self.reseed(note="dead-capacity", failure_class="capacity", harness="codex")
            self.route["selection_pins"] = {"owner": {"harness": "codex"}}
            self.route["work_request"]["owner_harness"] = "codex"
            reset = int(time.time()) + 86400
            windows = {"windows": [["7d", 100, reset]]}
            for metadata in ({"credits": {"has_credits": False, "unlimited": False}},
                             {"credits": {"has_credits": True}, "spend_control_reached": True}):
                self.assertTrue(usage_cache.record("codex", {**windows, **metadata}))
                waiting = self.start()
                self.assertEqual(waiting["state"], "waiting-capacity", waiting)
                self.assertEqual(waiting["reason"], "owner-capacity-wait")
                self.assertEqual((self.calls, self.claims()), ([], []))
            self.assertTrue(usage_cache.record("codex", {**windows,
                "credits": {"has_credits": True, "unlimited": False, "balance": "56446.1701575000"},
                "spend_control_reached": None}))
            result = self.start()
            self.assertEqual(result["state"], "running", result)
            self.assertEqual(len(self.calls), 1)
            self.assertEqual(len(self.claims()), 1)
            successor = self.replacement_id()
            self.assertEqual(self.rows()[successor][1]["harness"], "codex")
            self.start()
            self.assertEqual(self.replacement_id(), successor)
            self.assertEqual((len(self.calls), len(self.claims())), (1, 1))

    # -- 2: a cleanup-pending child is settled by start, not by hand ------------------
    def _owner_with_child(self, child_state=("unverifiable", "post-exit-receipt-incomplete", None)):
        self.reseed(note="dead-exact-pid", failure_class="contract")
        child = {**self.owner, "attempt_id": "att-child", "parent_attempt_id": "att-owner",
                 "worker_type": "stage", "dispatch_depth": "2", "route_node": "execute",
                 "note": "dead-exact-pid", "failure_class": "contract"}
        child.pop("replacement_input_digest")
        self.write(child)
        self.quiet["att-child"] = child_state

    def test_owner_with_cleanup_pending_child_is_replaced_by_one_start(self):
        self._owner_with_child()
        result = self.start()
        self.assertEqual(result["state"], "running", result)
        self.assertEqual(self.cleaned, [("att-child", True)])
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.rows()[self.replacement_id()][1]["automatic_retry_of"], "att-owner")

    def test_unproven_child_cleanup_still_blocks_the_replacement(self):
        self._owner_with_child()
        self.cleanup_settles = False
        self.ready = True
        result = self.start()
        self.assertEqual(result["reason"], "replacement-owner-child-unsettled", result)
        self.assertEqual((self.cleaned, self.calls, self.claims()), ([("att-child", True)], [], []))

    # -- 3: the sealed release is not the installed one --------------------------------
    def _drift_setup(self):
        import hearting_gates
        hearting_gates._REPORTED.clear()
        self.set_limit("att-owner", FREE)
        self.seal_under(self.root / "releases" / "vOLD")  # the old release folder does not exist
        self.candidate_args = {"model": "new-model"}

    def test_sealed_release_differs_from_installed_runtime_one_start_replaces(self):
        import contextlib
        import io
        self._drift_setup()
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            result = self.start()
        self.assertEqual(result["state"], "running", result)
        self.assertEqual(self.calls[0][1], str(R.ROOT / "utilities/dispatch-owner.py"))
        # The release a launch ran from is where it ran: no diagnostic, and the evidence stays.
        self.assertNotIn("replacement-runtime-drift", stderr.getvalue())
        replay = R.launch_input(self.jobs, "att-owner", self.rows()["att-owner"][1])
        self.assertEqual(Path(replay["launch_home"]).name, "vOLD")

    def test_identity_change_is_refused_even_when_the_release_differs(self):
        self._drift_setup()
        # Adapter settings may change with its harness; the work's profile may not.
        self.candidate_args = {"model_profile": "balanced"}
        self.ready = True
        result = self.start()
        self.assertEqual(result["state"], "needs-attention", result)
        self.assertIn(result["reason"], {"replacement-input-tuple-mismatch", "replacement-argv-mismatch"})
        self.assertFalse([a for a, (_, m) in self.rows().items() if m.get("automatic_retry_of")])

    def test_gates_on_a_release_drift_alone_still_replaces(self):
        self._drift_setup()
        with mock.patch.dict(os.environ, {"HEARTING_GATES": "on"}):
            result = self.start()
        self.assertEqual(result["state"], "running", result)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(len(self.claims()), 1)

    # -- 4: a claim that never launched continues, and says why it stalled -----------
    def test_pending_replacement_claim_resumes_with_one_start(self):
        self.set_limit("att-owner", FREE)
        self.launch_error = "boom"
        self.ready = True
        result = self.start()
        pending = result["replacement_attention"][0]
        self.assertEqual(pending["reason"], "replacement-launch-pending")
        self.assertIn("boom", pending["launcher_diagnostic"])
        claim = self.claims()
        self.assertEqual(len(claim), 1)
        self.launch_error = None
        self.ready = False
        result = self.start()
        self.assertEqual(result["state"], "running", result)
        self.assertEqual(self.claims(), claim)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.calls[0][2:], self.calls[1][2:])

    def claiming_launcher(self, stop_before_claim):
        """Like `launcher`, but through the real registry claim with a fresh lease nonce."""
        def run(command, **kwargs):
            self.calls.append(command)
            argv = command[2:]

            def value(flag):
                return argv[argv.index(flag) + 1]
            aid, prior = value("--attempt-id"), value("--automatic-retry-of")
            source = self.rows()[prior][1]
            args = SimpleNamespace(attempt_id=aid, jobs_path=self.jobs, worktree=value("--worktree"),
                                   route_id="rt-cap", route_node="owner", model_profile=source["model_profile"],
                                   replacement_input_argv=list(argv))
            meta = {k: v for k, v in source.items() if k not in {
                "note", "failure_class", "launch_outcome", "replacement_input_digest", "replacement_family_id",
                "replacement_attempt_id", "replacement_claim_digest", "replacement_original_attempt_id",
                "replacement_ordinal", "automatic_retry_of", "log_file", "cleanup_receipt_digest",
                "launch_started"}}
            meta.update(attempt_id=aid, automatic_retry_of=prior, log_file=str(self.root / (aid + ".log")),
                        supervisor_lease_nonce=os.urandom(16).hex())
            meta.update(D.parse_registry_metadata(R.seal_launch_input(
                args, source["harness"], Path(value("--prompt-file")).read_text())))
            stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            row = (stamp + "\topen\t" + str(self.root) + "\t" + str(self.root) + "\ttask\t"
                   + ",".join(k + "=" + v for k, v in meta.items()))
            claimed = D.claim_attempt_row(self.jobs, aid, row, launch=not stop_before_claim)
            if stop_before_claim:
                raise subprocess.TimeoutExpired(command, kwargs.get("timeout"), output="waiting on disk")
            return subprocess.CompletedProcess(command, 0 if claimed else 73, "child_spawned=1\n", "")
        return run

    def test_a_launcher_stopped_before_its_claim_relaunches_from_a_newer_release(self):
        # Cairn rt-bc71ede6fe68be41: the first start after the limit registered the
        # replacement and its launcher was killed before the claim; the next start
        # ran from a newer release. It must launch that same attempt.
        self.set_limit("att-owner", FREE)
        result = W.start_work(self.route, self.path, self.jobs, run=self.claiming_launcher(True))
        pending = result["replacement_attention"][0]
        self.assertEqual(pending["reason"], "replacement-launch-timeout", result)
        replacement = self.replacement_id()
        self.assertEqual(self.rows()[replacement][1]["launch_claimed"], "0")
        with mock.patch.object(R, "ROOT", self.root / "newer-release"):
            result = W.start_work(self.route, self.path, self.jobs, run=self.claiming_launcher(False))
        self.assertEqual(result["state"], "running", result)
        self.assertEqual(self.replacement_id(), replacement)
        self.assertEqual(self.rows()[replacement][1]["launch_claimed"], "1")
        self.assertEqual(len(self.claims()), 1)
        sealed = R.launch_input(self.jobs, replacement, self.rows()[replacement][1])
        self.assertEqual(sealed["launch_home"], str(self.root / "newer-release"))

    def test_transport_deaths_after_a_limit_continue_without_a_count_cutoff(self):
        # BC rt-6a54b9aabafc81bd: the limit stop is a pause, not the node's one
        # silent replacement. The resumed owner's own silent death is replaced once.
        self.set_limit("att-owner", FREE)
        self.assertEqual(self.start()["state"], "running")
        resumed = self.replacement_id()
        (self.root / (resumed + ".log")).write_text("")  # the crash left no result to settle
        self.set_status(resumed, "done", note="dead-runtime-exit", failure_class="runtime")
        self.statuses[resumed] = dict(verdict="FAIL", completion_proven=False, marker=None, marker_digest=None)
        result = self.start()
        second = self.replacement_id(resumed)
        self.assertNotEqual(second, resumed)
        self.assertEqual(len(self.calls), 2)
        # A second settled transport death keeps the same route and advances again.
        (self.root / (second + ".log")).write_text("")
        self.set_status(second, "done", note="dead-runtime-exit", failure_class="runtime")
        self.statuses[second] = dict(verdict="FAIL", completion_proven=False, marker=None, marker_digest=None)
        result = self.start()
        self.assertEqual(len(self.calls), 3, result)
        self.assertEqual(result["state"], "running", result)
        self.assertEqual(self.rows()[self.replacement_id(second)][1]["automatic_retry_of"], second)

    def test_a_crashed_owner_is_replaced_once_only_by_an_explicit_start(self):
        # rt-6c92471ab05995bf: the owner's process exited 70 before any result.
        self.reseed(note="dead-runtime-exit", failure_class="runtime")
        self.statuses["att-owner"] = dict(verdict="FAIL", completion_proven=False, marker=None, marker_digest=None)
        tick = R.advance(self.jobs, "att-owner", run=self.launcher, authority_check=lambda *a: True)
        self.assertEqual(tick["state"], "not-applicable")
        self.assertEqual(self.calls, [])
        self.start()
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.rows()[self.replacement_id()][1]["automatic_retry_of"], "att-owner")

    # The result row an OpenCode owner wrote when the provider answered 504 (not a capacity stop).
    PROVIDER_504 = json.dumps({"type": "result", "runtime": "opencode", "session_id": "s",
        "subtype": "error_during_execution", "is_error": True, "result": "",
        "error": {"name": "APIError", "data": {"message": "Upstream response was not valid JSON",
                                               "statusCode": 504, "isRetryable": True}}}) + "\n"

    def _provider_death(self, aid):
        (self.root / (aid + ".log")).write_text(self.PROVIDER_504)
        self.set_status(aid, "done", note="dead-runtime-error", failure_class="runtime",
                        terminal_event="opencode-result", process_exit="1")
        self.statuses[aid] = dict(verdict="FAIL", completion_proven=False, marker=None, marker_digest=None)

    def _provider_error_owner(self):
        self.reseed(note="dead-runtime-error", failure_class="runtime", harness="opencode")
        self._provider_death("att-owner")
        # Not vacuous: the real post-hoc reader sees a runtime error envelope, not an absent log.
        from codex_dispatch_terminal import inspect_terminal_attempt
        seen = inspect_terminal_attempt(str(self.root / "att-owner.log"), worktree=str(self.root),
                                        artifact_root_metadata=str(self.root / "artifacts"))
        self.assertEqual((seen["state"], seen["failure_class"]), ("invalid", "runtime"))

    def test_provider_errors_have_no_cumulative_replacement_cutoff(self):
        self._provider_error_owner()
        # a supervisor/rewake tick never launches it
        self.assertEqual(R.advance(self.jobs, "att-owner", run=self.launcher,
                                   authority_check=lambda *a: True)["state"], "not-applicable")
        R.advance_batch(self.jobs, {"att-owner"}, authority_check=lambda *a: True, run=self.launcher)
        self.assertEqual((self.calls, self.claims()), ([], []))
        # one explicit start replaces it once
        result = self.start()
        self.assertEqual(result["state"], "running", result)
        self.assertEqual((len(self.calls), len(self.claims())), (1, 1))
        first = self.replacement_id()
        self.assertEqual(self.rows()[first][1]["automatic_retry_of"], "att-owner")
        record = json.loads(self.claims()[0].read_text())
        self.assertEqual((record["proof"]["death_kind"], record["proof"]["note"]), ("runtime", "dead-runtime-error"))
        self.assertNotIn("after_capacity", record["logical_node"])
        original_claim = self.claims()[0].read_bytes()
        previous = first
        for count in range(2, 5):
            self._provider_death(previous)
            result = self.start()
            self.assertEqual((len(self.calls), len(self.claims())), (count, count), result)
            self.assertEqual(result["state"], "running", result)
            successor = self.replacement_id(previous)
            self.assertNotEqual(successor, previous)
            # Duplicate start observes that exact successor; it launches nobody.
            self.start()
            self.assertEqual(len(self.calls), count)
            previous = successor
        self.assertIn(original_claim, [path.read_bytes() for path in self.claims()])

    def test_old_exhaustion_resumes_same_route_and_keeps_plan_for_all_harnesses(self):
        native_errors = {
            "codex": [{"type": "dispatch.supervisor.turn.failed", "codex_error_info": "serverOverloaded",
                       "message": "Selected model is at capacity. Please try a different model."},
                      {"type": "dispatch.supervisor.terminal", "terminal": {
                       "note": "dead-runtime-error", "failure_class": "runtime", "process_exit": "70"}}],
            "claude": [{"type": "result", "session_id": "s", "is_error": True,
                        "subtype": "error_during_execution", "api_error_status": 503,
                        "result": "Provider unavailable"}],
            "opencode": [json.loads(self.PROVIDER_504)],
        }
        plan = self.root / "completed-plan.md"
        plan.write_bytes(b"completed original plan\n")
        reused = {"node": "plan", "attempt_id": "att-plan", "marker_digest": "sha256:plan"}
        for harness, events in native_errors.items():
            with self.subTest(harness=harness):
                self.calls.clear(); self.statuses.clear()
                self.reseed(note="dead-runtime-error", failure_class="runtime", harness=harness)
                self.route["selection_pins"] = {"owner": {"harness": harness}, "worker": {"harness": harness}}
                self.path.write_text(json.dumps(self.route))
                route_bytes = self.path.read_bytes()
                self.route["work_request"]["owner_harness"] = harness
                previous = "att-owner"
                history = []
                with mock.patch.object(R, "_reuse_snapshot", return_value={
                        "completed": [reused], "cycle_id": "cyc", "producer_id": "prod", "gates": []}):
                    for count in range(1, 5):
                        log = self.root / (previous + ".log")
                        log.write_text("".join(json.dumps(row) + "\n" for row in events))
                        self.set_status(previous, "done", note="dead-runtime-error", failure_class="runtime",
                                        process_exit="70")
                        self.statuses[previous] = dict(verdict="FAIL", completion_proven=False,
                                                      marker=None, marker_digest=None)
                        if count == 2:
                            # Reproduce the durable old-version attention without
                            # editing or deleting it during the upgraded start.
                            R.exhausted_attention(self.jobs, previous, self.rows()[previous][1])
                            history = [(path, path.read_bytes()) for path in
                                R._directory(self.jobs).joinpath("attention").rglob("*.json")]
                        with mock.patch.object(R, "ROOT", self.root / "installed-new-release"):
                            result = self.start()
                        self.assertEqual(result["state"], "running", result)
                        self.assertEqual((len(self.calls), len(self.claims())), (count, count))
                        successor = self.replacement_id(previous)
                        row = self.rows()[successor][1]
                        self.assertEqual((row["harness"], row["model_profile"]), (harness, "deep"))
                        self.assertEqual(row["route_id"], "rt-cap")
                        claim = json.loads(R._record_path(self.jobs, row["replacement_family_id"]).read_text())
                        self.assertEqual(claim["reuse"]["completed"], [reused])
                        self.assertTrue(all(path.read_bytes() == data for path, data in history))
                        self.assertEqual(plan.read_bytes(), b"completed original plan\n")
                        self.assertEqual(self.path.read_bytes(), route_bytes)
                        previous = successor
                # An owner's content FAIL still waits for a correction, regardless
                # of how many infrastructure replacements preceded it.
                self.set_status(previous, "done", note="dead-worker-fail", failure_class="fail")
                self.statuses[previous] = dict(verdict="FAIL", completion_proven=False,
                                              marker=None, marker_digest=None)
                result = self.start()
                self.assertEqual(result["reason"], "owner-failed", result)
                self.assertEqual(len(self.calls), 4)

    def test_a_provider_error_owner_with_a_live_or_unknown_child_is_not_replaced(self):
        for child_state, settles in ((("live", "attempt-descendant-live", 99), True),
                                     (("unverifiable", "post-exit-receipt-incomplete", None), False)):
            with self.subTest(child=child_state[0]):
                self._provider_error_owner()
                self.calls.clear()
                child = {**self.owner, "attempt_id": "att-child", "parent_attempt_id": "att-owner",
                         "worker_type": "stage", "dispatch_depth": "2", "route_node": "execute",
                         "note": "dead-exact-pid", "failure_class": "contract"}
                child.pop("replacement_input_digest")
                self.write(child)
                self.quiet["att-child"] = child_state
                self.cleanup_settles = settles
                self.ready = True
                result = self.start()
                self.assertEqual(result["reason"], "replacement-owner-child-unsettled", result)
                self.assertEqual((self.calls, self.claims()), ([], []))

    def test_claim_made_under_an_old_runtime_launches_under_the_installed_one(self):
        self._drift_setup()
        record = R.claim(self.jobs, "att-owner")
        self.assertEqual(len(self.claims()), 1)
        import contextlib
        import io
        with contextlib.redirect_stderr(io.StringIO()):
            result = self.start()
        self.assertEqual(result["state"], "running", result)
        self.assertEqual(self.replacement_id(), record["replacement_attempt_id"])
        self.assertEqual(len(self.claims()), 1)

    # -- 5: live work is never replaced --------------------------------------------------
    def test_live_owner_or_live_child_is_never_replaced(self):
        # (a) an owner that is still running
        self.set_status("att-owner", "open", note="registered", failure_class="pass")
        self.quiet["att-owner"] = ("live", "pid-live", 7)
        result = self.start()
        self.assertEqual(result["state"], "running", result)
        self.assertEqual((self.calls, self.claims()), ([], []))
        # (b) a finished owner whose tagged descendant is still alive
        self.reseed(note="dead-capacity", failure_class="capacity", result=FREE)
        self.quiet["att-owner"] = ("live", "attempt-descendant-live", 4242)
        self.ready = True
        result = self.start()
        stopped = result["replacement_attention"][0]
        self.assertEqual(stopped["reason"], "replacement-process-live", result)
        self.assertEqual((stopped["live_kind"], stopped["live_pids"], stopped["live_attempt_id"]),
                         ("tagged-descendant", ["4242"], "att-owner"))
        self.assertEqual((self.calls, self.claims(), self.cleaned), ([], [], []))
        # (c) a terminal child that is still alive is not touched either
        self._owner_with_child(("live", "attempt-descendant-live", 99))
        result = self.start()
        self.assertEqual(result["reason"], "replacement-owner-child-unsettled", result)
        self.assertEqual((self.calls, self.claims(), self.cleaned), ([], [], []))

    # -- 6: only an explicit start launches a capacity replacement -------------------
    def _state_files(self):
        base = R._directory(self.jobs)
        return sorted(str(p.relative_to(base)) for p in base.rglob("*") if p.is_file())

    def test_capacity_replacement_launches_only_from_explicit_start(self):
        self.route["selection_pins"] = {"owner": {"harness": "claude"}}
        self.set_limit("att-owner", FREE)
        # (a) the limit has lifted, but a supervisor/rewake tick does not launch
        before = self._state_files()
        for check in (None, lambda *_: True):
            effective, lineage, attention = R.advance_batch(
                self.jobs, {"att-owner"}, authority_check=check, run=self.launcher)
            self.assertEqual((effective, lineage), ({"att-owner"}, []))
            self.assertEqual([a["reason"] for a in attention], ["replacement-capacity-wait"])
        self.assertEqual((self.calls, self.claims(), self._state_files()), ([], [], before))
        # (b) one start does
        self.assertEqual(self.start()["state"], "running")
        first = self.replacement_id()
        # the launched replacement is left alone by automatic ticks: no attention, no new launch
        launched = len(self.calls)
        effective, lineage, attention = R.advance_batch(
            self.jobs, {"att-owner"}, authority_check=lambda *_: True, run=self.launcher)
        self.assertEqual(attention, [])
        self.assertEqual(len(lineage), 1)
        self.assertEqual(len(self.calls), launched)
        # (c) the replacement stops at a limit as well: waits while limited, then resumes as a new family
        self.set_status(first, "done", note="dead-capacity", failure_class="capacity", launch_claimed="1")
        self.set_limit(first, LIMITED)
        waiting = self.start()
        self.assertEqual(waiting["state"], "waiting-capacity", waiting)
        self.assertEqual(len(self.claims()), 1)
        self.set_limit(first, FREE)
        self.assertEqual(self.start()["state"], "running")
        second = self.replacement_id(first)
        self.assertEqual(len(self.claims()), 2)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.rows()[second][1]["automatic_retry_of"], first)
        # (d) with no hold evidence left, repeated automatic ticks still launch nothing
        self.set_status(second, "done", note="dead-capacity", failure_class="capacity", launch_claimed="1")
        self.set_limit(second, "resets nothing we can read")
        with mock.patch("dispatch_capacity_evidence.harness_hold", return_value=None):
            for _ in range(3):
                for attempts in ({"att-owner"}, {first}, {second}):
                    _, _, attention = R.advance_batch(self.jobs, attempts, authority_check=lambda *_: True,
                                                      run=self.launcher)
                    self.assertEqual([a["reason"] for a in attention if a["source_attempt_id"] == second],
                                     ["replacement-capacity-wait"] * (second in attempts))
        self.assertEqual((len(self.calls), len(self.claims())), (2, 2))


if __name__ == "__main__":
    unittest.main()
