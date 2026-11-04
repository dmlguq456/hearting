#!/usr/bin/env python3
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "utilities"))
import dispatch_contract as D
from split_module_fixture import patch_split

WATCH = ROOT / "utilities" / "dispatch-reap-watch.py"
CURRENT = (
    "attempt_schema_version=2,dispatch_depth=2,transport=headless,"
    "execution_surface=registered-headless,registered_worker=1,"
    "fallback_hop=same-harness-headless,worker_type=stage"
)
OWNER = (
    "attempt_schema_version=2,dispatch_depth=1,transport=headless,"
    "execution_surface=registered-headless,registered_worker=1,"
    "fallback_hop=same-harness-headless,worker_type=owner"
)


def _row(status, repo, worktree, slug, meta_str):
    return f"2026-08-13T00:00:00Z\t{status}\t{repo}\t{worktree}\t{slug}\t{meta_str}"


def _registry_with_parent_and_leg(
    base, parent_proc, leg_proc, *, parent_open=True, foreign=None
):
    """Write a two-row registry sharing worktree/repo/slug and parent linkage.

    `foreign` selects one deliberately-broken parent axis for the
    not-a-completion-window fixtures: "worktree", "pid_start", or None
    (leaves ``parent_open`` alone to model a plain terminal parent row).
    """

    parent_identity = D.process_launch_identity(parent_proc.pid)
    leg_identity = D.process_launch_identity(leg_proc.pid)
    repo, worktree, slug = "/repo", "/wt", "owner"

    parent_meta = dict(parent_identity)
    parent_meta["attempt_id"] = "att-parent"
    parent_meta["launch_lifecycle"] = "registered"
    if foreign == "pid_start":
        parent_meta["pid_start"] = "1"

    leg_meta = dict(leg_identity)
    leg_meta["attempt_id"] = "att-leg"
    leg_meta["launch_lifecycle"] = "detached"
    leg_meta["parent"] = slug
    leg_meta["parent_attempt_id"] = "att-parent"
    leg_meta["log_file"] = str(base / "missing.jsonl")

    parent_worktree = "/wt-foreign" if foreign == "worktree" else worktree
    parent_status = "open" if parent_open else "done"

    jobs = base / "jobs.log"
    jobs.write_text(
        _row(
            parent_status,
            repo,
            parent_worktree,
            slug,
            OWNER + "," + ",".join(f"{k}={v}" for k, v in parent_meta.items()),
        )
        + "\n"
        + _row(
            "open",
            repo,
            worktree,
            "leg",
            CURRENT + "," + ",".join(f"{k}={v}" for k, v in leg_meta.items()),
        )
        + "\n",
        encoding="utf-8",
    )
    return jobs


class DispatchReapWatchTest(unittest.TestCase):
    def setUp(self):
        self.fixture = tempfile.TemporaryDirectory(prefix="dispatch-reap-watch-")
        self.addCleanup(self.fixture.cleanup)
        root = Path(self.fixture.name)
        home = root / "home"
        for path in (home, root / "state", root / "config", root / "data",
                     root / "harness-state", root / "artifacts", root / "peer-ledger"):
            path.mkdir(parents=True, exist_ok=True)
        jobs = root / "jobs.log"
        jobs.touch()
        fixture_env = dict(os.environ)
        for key in tuple(fixture_env):
            if (key.startswith("AGENT_DISPATCH_")
                    or key.startswith("AGENT_ARTIFACT_")
                    or key in {"AGENT_HOME", "CLAUDE_HOME", "AGENT_RUNTIME_ROOT",
                               "AGENT_SESSION_ROLE", "HERDR_SOCKET_PATH",
                               "AGENT_HERDR_SESSION", "HERDR_SESSION",
                               "SERVICE_CURRENT", "HEARTING_SERVICE_CURRENT",
                               "AGENT_SERVICE_CURRENT", "AGENT_INSTALL_CURRENT",
                               "AGENT_INSTALL_ROOT"}):
                fixture_env.pop(key, None)
        fixture_env.update({
            "HOME": str(home), "XDG_STATE_HOME": str(root / "state"),
            "XDG_CONFIG_HOME": str(root / "config"), "XDG_DATA_HOME": str(root / "data"),
            "HARNESS_STATE_ROOT": str(root / "harness-state"),
            "HARNESS_DATA_ROOT": str(root / "data"),
            "AGENT_HOME": str(root / "agent-home"),
            "CLAUDE_HOME": str(root / "claude-home"),
            "AGENT_RUNTIME_ROOT": str(root / "runtime"),
            "AGENT_ARTIFACT_ROOT": str(root / "artifacts"),
            "AGENT_ARTIFACT_OUTPUT_DIR": str(root / "artifacts" / "output"),
            "AGENT_PEER_LEDGER_ROOT": str(root / "peer-ledger"),
            "AGENT_DISPATCH_JOBS": str(jobs),
            "CODEX_HOME": str(home / ".codex"),
            "CLAUDE_CONFIG_DIR": str(home / ".claude"),
            "OPENCODE_CONFIG_DIR": str(home / ".config" / "opencode"),
        })
        patcher = mock.patch.dict(os.environ, fixture_env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_launcher_strips_attempt_tag_from_observer_environment(self):
        fake = mock.Mock(pid=37)
        with mock.patch.dict(os.environ, {
            D.ATTEMPT_DESCENDANT_ENV: "att-observer-self", "HEARTING_FIXTURE_KEEP": "1",
        }), mock.patch.object(D.subprocess, "Popen", return_value=fake) as spawn:
            self.assertEqual(os.environ.get(D.ATTEMPT_DESCENDANT_ENV), "att-observer-self")
            self.assertEqual(
                D.launch_reap_watch(
                    Path("/tmp/jobs.log"), "att-observer-self", 19, "200", 19
                ),
                37,
            )
            self.assertEqual(os.environ.get(D.ATTEMPT_DESCENDANT_ENV), "att-observer-self")
        self.assertNotIn(D.ATTEMPT_DESCENDANT_ENV, spawn.call_args.kwargs["env"])
        self.assertEqual(spawn.call_args.kwargs["env"]["HEARTING_FIXTURE_KEEP"], "1")

    def test_governance_environment_copies_parent_or_service_environment(self):
        with mock.patch.dict(os.environ, {
            D.ATTEMPT_DESCENDANT_ENV: "att-parent", "HEARTING_FIXTURE_KEEP": "parent",
        }, clear=True):
            parent_before = dict(os.environ)
            parent_env = D.governance_environment()
            self.assertEqual(parent_env, {"HEARTING_FIXTURE_KEEP": "parent"})
            self.assertEqual(dict(os.environ), parent_before)

        self.assertEqual(D.governance_environment({}), {})
        service = {
            D.ATTEMPT_DESCENDANT_ENV: "att-service",
            "AGENT_HOME": "/fixture/releases/B",
            "HEARTING_FIXTURE_KEEP": "service",
        }
        service_before = dict(service)
        self.assertEqual(D.governance_environment(service), {
            "AGENT_HOME": "/fixture/releases/B", "HEARTING_FIXTURE_KEEP": "service",
        })
        self.assertEqual(service, service_before)

    def test_foreground_launch_requires_the_locked_seal_and_rejects_stale_digest(self):
        with tempfile.TemporaryDirectory() as td:
            jobs = Path(td) / "jobs.log"
            jobs.write_text(
                _row(
                    "open", "/repo", "/wt", "review",
                    "attempt_schema_version=2,dispatch_depth=1,transport=headless,"
                    "execution_surface=registered-headless,registered_worker=1,"
                    "fallback_hop=same-harness-headless,worker_type=review,"
                    "launch_lifecycle=foreground-scoped,attempt_id=att-fence,"
                    "pid=123,pid_start=456,pgid=123,pid_ns=pid:[1],"
                    "pid_observer_ns=pid:[1]",
                ) + "\n",
                encoding="utf-8",
            )
            with self.assertRaises(D.DispatchContractError) as missing:
                D.launch_reap_watch(jobs, "att-fence", 123, "456", 123)
            self.assertEqual(missing.exception.reason, "reap-watch-foreground-seal-required")
            seal = D.seal_foreground_result(
                jobs, "att-fence", 123, "456", 123,
                exit_code=7, failure="exit-7", group_empty=True,
            )
            stale = D.SealedForegroundOutcome(seal.values, "0" * 64)
            with self.assertRaises(D.DispatchContractError) as invalid:
                D.launch_reap_watch(
                    jobs, "att-fence", 123, "456", 123,
                    foreground_seal=stale,
                )
            self.assertEqual(invalid.exception.reason, "reap-watch-foreground-seal-invalid")
            with mock.patch.object(D.subprocess, "Popen", return_value=mock.Mock(pid=41)):
                self.assertEqual(
                    D.launch_reap_watch(
                        jobs, "att-fence", 123, "456", 123,
                        foreground_seal=seal,
                    ),
                    41,
                )

    def test_detached_watcher_waits_for_escaped_tagged_child_and_seals_receipt(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            jobs = base / "jobs.log"
            attempt = "att-detached-reap-watch"
            script = (
                "import os,subprocess,sys,time\n"
                "env=dict(os.environ,AGENT_DISPATCH_ATTEMPT_ID=sys.argv[1])\n"
                "subprocess.Popen(['sleep','0.45'],env=env,start_new_session=True)\n"
                "time.sleep(0.04)\n"
            )
            env = dict(os.environ, AGENT_DISPATCH_ATTEMPT_ID=attempt)
            worker = subprocess.Popen(
                [sys.executable, "-c", script, attempt],
                env=env,
                start_new_session=True,
            )
            identity = D.process_launch_identity(worker.pid)
            metadata = ",".join(
                f"{key}={value}"
                for key, value in {
                    **identity,
                    "attempt_id": attempt,
                    "launch_lifecycle": "detached",
                }.items()
            )
            jobs.write_text(
                "2026-08-09T00:00:00Z\tdone\t/repo\t/wt\tworker\t"
                f"{CURRENT},{metadata},note=completed-marker\n",
                encoding="utf-8",
            )
            watcher = subprocess.Popen(
                [
                    sys.executable,
                    str(WATCH),
                    "--jobs", str(jobs),
                    "--attempt-id", attempt,
                    "--pid", str(worker.pid),
                    "--pid-start", identity["pid_start"],
                    "--pgid", identity["pgid"],
                    "--interval", "0.02",
                ]
            )
            worker.wait(timeout=5)
            time.sleep(0.08)
            self.assertIsNone(watcher.poll(), "watcher ignored escaped descendant")
            self.assertEqual(watcher.wait(timeout=10), 0)
            row = jobs.read_text(encoding="utf-8")
            self.assertIn("launch_outcome=governed-process-group-drained", row)
            self.assertIn(f"group_reap_proof={D.GROUP_REAP_PROOF}", row)
            self.assertIn(f"group_reap_pgid={identity['pgid']}", row)
            self.assertIn(
                f"attempt_descendant_proof={D.ATTEMPT_DESCENDANT_PROOF}", row
            )
            self.assertIn(
                f"attempt_descendant_observer_ns={identity['pid_observer_ns']}", row
            )
            self.assertFalse((base / "degradations").exists())

    def _tagged_residue_worker(self, attempt, residue_seconds="30", *, resource=False, ignore_term=False):
        """Spawn a worker that leaves one tagged, re-setsid'd survivor behind."""

        script = (
            "import os,subprocess,sys,time\n"
            "env=dict(os.environ,AGENT_DISPATCH_ATTEMPT_ID=sys.argv[1])\n"
            "subprocess.Popen(['sleep',sys.argv[2]],env=env,start_new_session=True)\n"
            "time.sleep(0.04)\n"
        )
        if ignore_term:
            payload = "import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);print('ready',flush=True);time.sleep(30)"
            script = ("import os,subprocess,sys\n"
                      f"p=subprocess.Popen([sys.executable,'-c',{payload!r}],env=os.environ,start_new_session=True,stdout=subprocess.PIPE)\n"
                      "assert p.stdout.readline()==b'ready\\n'\n"
                      "p.stdout.close()\n")
        worker = subprocess.Popen(
            [sys.executable, "-c", script, attempt, residue_seconds],
            env=dict(os.environ, AGENT_DISPATCH_ATTEMPT_ID=attempt,
                     **({'HEARTING_RESOURCE_RUN_ID': 'preserved-training'} if resource else {})),
            start_new_session=True,
        )
        identity = D.process_launch_identity(worker.pid)
        worker.wait(timeout=5)

        def reap_residue():
            probe = D.attempt_tagged_descendants({"attempt_id": attempt, **identity})
            for pid, _start, _state in probe.members:
                try:
                    os.kill(pid, 9)
                except OSError:
                    pass

        self.addCleanup(reap_residue)
        return identity

    def test_terminal_tagged_residue_is_reaped_and_cleanup_settles(self):
        """H7 (rt-8561efa2 / att-e72e08e0): a worker-spawned background process
        that inherited the attempt tag outlived the worker. The sidecar rescanned
        /proc every 0.2 s for 95 minutes and the row could never drain."""

        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            jobs = base / "jobs.log"
            attempt = "att-tagged-residue-terminal"
            identity = self._tagged_residue_worker(attempt, ignore_term=True)
            metadata = ",".join(
                f"{key}={value}"
                for key, value in {
                    **identity,
                    "attempt_id": attempt,
                    "launch_lifecycle": "detached",
                    "pid_scope": "namespace-local",
                }.items()
            )
            jobs.write_text(
                "2026-08-09T00:00:00Z\tdone\t/repo\t/wt\tworker\t"
                f"{CURRENT},{metadata},note=completed-marker\n",
                encoding="utf-8",
            )
            started = time.monotonic()
            watcher = subprocess.Popen(
                [
                    sys.executable, str(WATCH),
                    "--jobs", str(jobs),
                    "--attempt-id", attempt,
                    "--pid", identity["pid"],
                    "--pid-start", identity["pid_start"],
                    "--pgid", identity["pgid"],
                    "--interval", "0.02",
                    "--residue-grace", "0.3",
                ]
            )
            self.assertEqual(watcher.wait(timeout=10), 0)
            self.assertLess(time.monotonic() - started, 8.0)
            row = jobs.read_text(encoding="utf-8")
            meta = D.parse_registry_metadata(row.strip().split("\t")[5])
            self.assertEqual(meta["launch_outcome"], "governed-process-group-drained")
            self.assertEqual(meta["attempt_descendant_proof"], D.ATTEMPT_DESCENDANT_PROOF)
            live = D.attempt_tagged_descendants(meta)
            self.assertEqual(live.state, "empty", "terminal residue must be gone")
            self.assertFalse(D.tagged_residue_receipt(meta))
            self.assertEqual(D.post_exit_receipt_reason(meta), "governed-process-group-drained")
            verdict = D.attempt_process_quiescence(meta, terminal_receipt=True)
            self.assertEqual(verdict.state, "quiescent", verdict.reason)
            before=jobs.read_bytes()
            self.assertTrue(D.resolve_attempt_cleanup(jobs,attempt,apply=True)["settled"])
            self.assertEqual(jobs.read_bytes(),before)

    def _open_residue_row(self, base, attempt, residue_seconds):
        """Open row whose worker finished (terminal envelope) leaving a tagged
        survivor; run the reap watcher until it seals the residue proof."""

        jobs = base / "jobs.log"
        log = base / f"{attempt}.claude.jsonl"
        log.write_text('{"type":"result","subtype":"success"}\n', encoding="utf-8")
        # Legacy residue recovery remains necessary for preserved resources.
        identity = self._tagged_residue_worker(attempt, residue_seconds, resource=True)
        metadata = ",".join(
            f"{key}={value}"
            for key, value in {
                **identity,
                "attempt_id": attempt,
                "launch_lifecycle": "detached",
                "pid_scope": "namespace-local",
                "log_file": str(log),
            }.items()
        )
        jobs.write_text(
            "2026-08-09T00:00:00Z\topen\t/repo\t/wt\tworker\t"
            f"{CURRENT},{metadata}\n",
            encoding="utf-8",
        )
        watcher = subprocess.Popen(
            [
                sys.executable, str(WATCH),
                "--jobs", str(jobs),
                "--attempt-id", attempt,
                "--pid", identity["pid"],
                "--pid-start", identity["pid_start"],
                "--pgid", identity["pgid"],
                "--interval", "0.02",
                "--residue-grace", "0.3",
            ]
        )
        self.assertEqual(watcher.wait(timeout=10), 0)
        return jobs

    @staticmethod
    def _row_metadata(jobs):
        return D.parse_registry_metadata(
            jobs.read_text(encoding="utf-8").strip().split("\t")[5]
        )

    def test_applied_terminal_cleanup_reaps_old_residue_preserves_result_and_other_attempt(self):
        with tempfile.TemporaryDirectory() as td:
            jobs = Path(td) / 'jobs.log'
            attempt = 'att-snapshot-cleanup'
            identity = self._tagged_residue_worker(attempt)
            other = self._tagged_residue_worker('att-unrelated-cleanup')
            metadata = ','.join(f'{key}={value}' for key, value in {
                **identity, 'attempt_id': attempt, 'launch_lifecycle': 'detached',
                'pid_scope': 'namespace-local', 'note': 'completed-supervisor',
                'failure_class': 'pass',
            }.items())
            jobs.write_text(f'2026-10-09T00:00:00Z\tdone\t/repo\t/wt\tworker\t{CURRENT},{metadata}\n')
            before = jobs.read_bytes()
            self.assertEqual(D.resolve_attempt_cleanup(jobs, attempt)['reason'], 'attempt-descendant-live')
            self.assertEqual(jobs.read_bytes(), before)
            self.assertTrue(D.resolve_attempt_cleanup(jobs, attempt, apply=True)['settled'])
            self.assertEqual(D.attempt_tagged_descendants(self._row_metadata(jobs)).state, 'empty')
            self.assertEqual(D.attempt_tagged_descendants({'attempt_id': 'att-unrelated-cleanup', **other}).state, 'populated')
            self.assertEqual(self._row_metadata(jobs)['failure_class'], 'pass')
            self.assertEqual(self._row_metadata(jobs)['note'], 'completed-supervisor')

    def test_terminal_cleanup_never_signals_live_leader_or_foreign_namespace(self):
        from unittest import mock
        with tempfile.TemporaryDirectory() as td:
            jobs = Path(td) / 'jobs.log'
            proc = subprocess.Popen(['sleep', '30'], start_new_session=True,
                                    env=dict(os.environ, AGENT_DISPATCH_ATTEMPT_ID='att-live-owner'))
            self.addCleanup(lambda: (proc.kill(), proc.wait()) if proc.poll() is None else None)
            identity = D.process_launch_identity(proc.pid)
            metadata = ','.join(f'{key}={value}' for key, value in {
                **identity, 'attempt_id': 'att-live-owner', 'launch_lifecycle': 'detached',
                'pid_scope': 'namespace-local',
            }.items())
            fields = ['2026-10-09T00:00:00Z', 'done', '/repo', '/wt', 'worker', f'{CURRENT},{metadata}']
            jobs.write_text('\t'.join(fields) + '\n')
            import route_parent_close as CLOSE
            with mock.patch.object(CLOSE, '_signal') as send:
                CLOSE.reap_terminal_descendants(jobs, fields)
                send.assert_not_called()
                with patch_split(D, 'attempt_scan_namespace_authority', return_value=False):
                    CLOSE.reap_terminal_descendants(jobs, fields)
                send.assert_not_called()
            self.assertIsNone(proc.poll())

    def test_residue_that_later_exits_is_released_by_the_runtime_not_left_draining(self):
        """stale-residue-1002 (BC [32], att-execute-e2584831102a-c3): the
        watcher sealed `attempt-tagged-residue-v1`, the survivor later exited,
        and nothing re-observed: the row stayed `post-exit-receipt-incomplete`
        for 2.5 h until the watcher was rerun by hand."""

        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            attempt = "att-residue-later-exits"
            # Leave a wide observation window after the watcher starts. A
            # short-lived survivor can exit during procfs scanning and turn
            # the intended residue fixture into a first-observation-empty
            # fixture before the residue receipt is sealed.
            jobs = self._open_residue_row(base, attempt, "5")
            meta = self._row_metadata(jobs)
            self.assertEqual(
                meta["attempt_descendant_proof"], D.ATTEMPT_DESCENDANT_RESIDUE_PROOF
            )
            self.assertEqual(meta["attempt_descendant_residue_basis"], "terminal-envelope")
            deadline = time.monotonic() + 12
            while D.attempt_tagged_descendants(meta).state != "empty":
                self.assertLess(time.monotonic(), deadline, "residue never exited")
                time.sleep(0.05)

            # Today's stall: the residue is gone, yet the receipt gate still
            # answers incomplete because only the empty proof releases it.
            verdict = D.attempt_process_quiescence(meta, terminal_receipt=True)
            self.assertEqual(
                (verdict.state, verdict.reason),
                ("unverifiable", "post-exit-receipt-incomplete"),
            )
            self.assertEqual(
                D.observed_attempt_liveness("open", meta, terminal_envelope=True).state,
                "unverifiable",
            )

            # The fix: the runtime repeats the watcher's own observation.
            before = jobs.read_bytes()
            dry = D.resolve_residue_drain(jobs, attempt)
            self.assertEqual((dry["state"], dry["changed"]), ("drained", False))
            self.assertEqual(jobs.read_bytes(), before)
            applied = D.resolve_residue_drain(jobs, attempt, apply=True)
            self.assertEqual((applied["state"], applied["changed"]), ("drained", True))
            fresh = self._row_metadata(jobs)
            self.assertEqual(fresh["attempt_descendant_proof"], D.ATTEMPT_DESCENDANT_PROOF)
            self.assertEqual(
                fresh["attempt_descendant_observer_ns"], meta["pid_observer_ns"]
            )
            for key in (
                "attempt_descendant_residue",
                "attempt_descendant_residue_count",
                "attempt_descendant_residue_basis",
                "attempt_descendant_residue_at",
            ):
                self.assertEqual(fresh[key], meta[key], key)
            verdict = D.attempt_process_quiescence(fresh, terminal_receipt=True)
            self.assertEqual(verdict.state, "quiescent", verdict.reason)
            self.assertEqual(
                D.observed_attempt_liveness("open", fresh, terminal_envelope=True).state,
                "reconcile-needed",
            )
            again = D.resolve_residue_drain(jobs, attempt, apply=True)
            self.assertEqual((again["changed"], again["reason"]),
                             (False, "residue-receipt-absent"))

    def test_live_residue_is_reported_not_upgraded(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            attempt = "att-residue-still-live"
            jobs = self._open_residue_row(base, attempt, "30")
            meta = self._row_metadata(jobs)
            live = D.attempt_tagged_descendants(meta)
            self.assertEqual(live.state, "populated")
            expected = [pid for pid, _s, _st in live.members]
            before = jobs.read_bytes()
            outcome = D.resolve_residue_drain(jobs, attempt, apply=True)
            self.assertEqual((outcome["state"], outcome["changed"]), ("residue-live", False))
            self.assertEqual(outcome["live_pids"], expected)
            self.assertEqual(jobs.read_bytes(), before)
            self.assertEqual(list(D.residue_live_pids(meta)), expected)

    def test_terminal_residue_inside_the_governed_group_is_reaped(self):
        """review finding 11: `nohup cmd &` without setsid keeps the governed
        pgid; when every live group member carries the tag it is residue."""
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            jobs = base / "jobs.log"
            attempt = "att-tagged-residue-ingroup"
            script = (
                "import os,subprocess,sys,time\n"
                "env=dict(os.environ,AGENT_DISPATCH_ATTEMPT_ID=sys.argv[1])\n"
                "subprocess.Popen(['sleep','30'],env=env)\n"   # same session and group
                "time.sleep(0.04)\n"
            )
            worker = subprocess.Popen(
                [sys.executable, "-c", script, attempt],
                env=dict(os.environ, AGENT_DISPATCH_ATTEMPT_ID=attempt),
                start_new_session=True,
            )
            identity = D.process_launch_identity(worker.pid)
            worker.wait(timeout=5)

            def reap_residue():
                for pid, _s, _st in D.attempt_tagged_descendants({"attempt_id": attempt, **identity}).members:
                    try:
                        os.kill(pid, 9)
                    except OSError:
                        pass

            self.addCleanup(reap_residue)
            metadata = ",".join(
                f"{key}={value}"
                for key, value in {**identity, "attempt_id": attempt, "launch_lifecycle": "detached"}.items()
            )
            jobs.write_text(
                "2026-08-09T00:00:00Z\tdone\t/repo\t/wt\tworker\t"
                f"{CURRENT},{metadata},note=completed-marker\n",
                encoding="utf-8",
            )
            self.assertEqual(D.process_group_observation(int(identity["pgid"])).state, "populated")
            watcher = subprocess.Popen(
                [
                    sys.executable, str(WATCH),
                    "--jobs", str(jobs),
                    "--attempt-id", attempt,
                    "--pid", identity["pid"],
                    "--pid-start", identity["pid_start"],
                    "--pgid", identity["pgid"],
                    "--interval", "0.02",
                    "--residue-grace", "0.3",
                ]
            )
            self.assertEqual(watcher.wait(timeout=10), 0)
            meta = D.parse_registry_metadata(jobs.read_text(encoding="utf-8").strip().split("\t")[5])
            self.assertEqual(meta["attempt_descendant_proof"], D.ATTEMPT_DESCENDANT_PROOF)
            self.assertFalse(D.tagged_residue_receipt(meta))

    def test_sd_open_47_tagged_residue_without_terminal_evidence_keeps_the_veto(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            jobs = base / "jobs.log"
            attempt = "att-tagged-residue-open"
            identity = self._tagged_residue_worker(attempt, residue_seconds="1.2")
            metadata = ",".join(
                f"{key}={value}"
                for key, value in {
                    **identity,
                    "attempt_id": attempt,
                    "launch_lifecycle": "detached",
                    "log_file": str(base / "missing.jsonl"),
                }.items()
            )
            jobs.write_text(
                "2026-08-09T00:00:00Z\topen\t/repo\t/wt\tworker\t"
                f"{CURRENT},{metadata}\n",
                encoding="utf-8",
            )
            watcher = subprocess.Popen(
                [
                    sys.executable, str(WATCH),
                    "--jobs", str(jobs),
                    "--attempt-id", attempt,
                    "--pid", identity["pid"],
                    "--pid-start", identity["pid_start"],
                    "--pgid", identity["pgid"],
                    "--interval", "0.02",
                    "--residue-grace", "0.1",
                ]
            )
            time.sleep(0.6)
            self.assertIsNone(
                watcher.poll(), "open row without terminal evidence must keep waiting"
            )
            self.assertEqual(watcher.wait(timeout=10), 0)
            row = jobs.read_text(encoding="utf-8")
            self.assertIn(f"attempt_descendant_proof={D.ATTEMPT_DESCENDANT_PROOF}", row)
            self.assertNotIn("attempt_descendant_residue=", row)

    def test_open_missing_result_is_closed_after_exact_group_drain(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            jobs = base / "jobs.log"
            attempt = "att-detached-missing-result"
            worker = subprocess.Popen(
                ["sleep", "0.08"],
                env={**os.environ, D.ATTEMPT_DESCENDANT_ENV: attempt},
                start_new_session=True,
            )
            identity = D.process_launch_identity(worker.pid)
            metadata = ",".join(
                f"{key}={value}"
                for key, value in {
                    **identity,
                    "attempt_id": attempt,
                    "launch_lifecycle": "detached",
                    "log_file": str(base / "missing.jsonl"),
                    "route_id": "rt-reap-leg",
                    "route_hash": "sha256:reap-leg",
                    "route_node": "impl-review-alternative",
                    "fallback_ordinal": "1",
                    "parallel_group": "impl-review",
                    "batch_declared_size": "2",
                    "batch_parallel_leg_index": "1",
                    "harness": "codex",
                    "leg_class": "peer",
                }.items()
            )
            jobs.write_text(
                "2026-08-09T00:00:00Z\topen\t/repo\t/wt\tworker\t"
                f"{CURRENT},{metadata}\n",
                encoding="utf-8",
            )
            watcher = subprocess.Popen(
                [
                    sys.executable,
                    str(WATCH),
                    "--jobs", str(jobs),
                    "--attempt-id", attempt,
                    "--pid", str(worker.pid),
                    "--pid-start", identity["pid_start"],
                    "--pgid", identity["pgid"],
                    "--interval", "0.02",
                ],
            )
            worker.wait(timeout=5)
            self.assertEqual(watcher.wait(timeout=5), 0)
            row = jobs.read_text(encoding="utf-8")
            self.assertIn("\tdone\t", row)
            self.assertIn("note=dead-missing-result", row)
            self.assertIn("terminal_event=dispatch-reap-missing-result", row)
            ledger = base / "degradations" / "rt-reap-leg.jsonl"
            with ledger.open(encoding="utf-8") as stream:
                events = [json.loads(line) for line in stream]
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["writer"], "dispatch-reap-watch.py")
            self.assertEqual(events[0]["kind"], "leg-failure")
            self.assertEqual(events[0]["route_id"], "rt-reap-leg")
            self.assertEqual(events[0]["route_hash"], "sha256:reap-leg")
            self.assertEqual(events[0]["route_node"], "impl-review-alternative")
            self.assertEqual(events[0]["parallel_group"], "impl-review")
            self.assertEqual(events[0]["parallel_leg_index"], "1")
            self.assertEqual(events[0]["parallel_leg_count"], "2")
            self.assertEqual(events[0]["attempt_id"], attempt)
            self.assertEqual(events[0]["reason"], "dead-missing-result")

            replay = subprocess.run(
                [
                    sys.executable,
                    str(WATCH),
                    "--jobs", str(jobs),
                    "--attempt-id", attempt,
                    "--pid", str(worker.pid),
                    "--pid-start", identity["pid_start"],
                    "--pgid", identity["pgid"],
                    "--interval", "0.02",
                ],
                check=False,
            )
            self.assertEqual(replay.returncode, 0)
            with ledger.open(encoding="utf-8") as stream:
                replayed_events = [json.loads(line) for line in stream]
            self.assertEqual(replayed_events, events)

    def test_opencode_usage_limit_death_closes_capacity(self):
        # plan.md item 5: an OpenCode attempt that leaves no result envelope
        # closes as dead-capacity (not the generic dead-missing-result) when
        # its exact session binds to one ERROR line in the OpenCode server
        # log naming a usage-limit death.
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            jobs = base / "jobs.log"
            attempt = "att-opencode-capacity"
            attempt_log = base / f"{attempt}.opencode.jsonl"
            attempt_log.write_text(
                json.dumps({"type": "step_start", "sessionID": "ses_cap111"}) + "\n",
                encoding="utf-8",
            )
            server_log_dir = (
                base / ".dispatch" / "opencode-runtime" / attempt
                / "data" / "opencode" / "log"
            )
            server_log_dir.mkdir(parents=True)
            server_log = server_log_dir / "opencode.log"
            server_log.write_text(
                json.dumps({
                    "level": "ERROR", "time": "2026-08-09T00:00:05Z",
                    "session": {"id": "ses_cap111"},
                    "error": {"error": "Monthly usage limit reached. Resets in 13 days"},
                }) + "\n",
                encoding="utf-8",
            )
            worker = subprocess.Popen(
                ["sleep", "0.08"],
                env={**os.environ, D.ATTEMPT_DESCENDANT_ENV: attempt},
                start_new_session=True,
            )
            identity = D.process_launch_identity(worker.pid)
            metadata = ",".join(
                f"{key}={value}"
                for key, value in {
                    **identity,
                    "attempt_id": attempt,
                    "launch_lifecycle": "detached",
                    "log_file": str(attempt_log),
                    "harness": "opencode",
                }.items()
            )
            jobs.write_text(
                f"2026-08-09T00:00:00Z\topen\t{base}\t{base}\tworker\t"
                f"{CURRENT},{metadata}\n",
                encoding="utf-8",
            )
            watcher = subprocess.Popen(
                [
                    sys.executable,
                    str(WATCH),
                    "--jobs", str(jobs),
                    "--attempt-id", attempt,
                    "--pid", str(worker.pid),
                    "--pid-start", identity["pid_start"],
                    "--pgid", identity["pgid"],
                    "--interval", "0.02",
                ],
            )
            worker.wait(timeout=5)
            self.assertEqual(watcher.wait(timeout=5), 0)
            row = jobs.read_text(encoding="utf-8")
            self.assertIn("\tdone\t", row)
            self.assertIn("note=dead-capacity", row)
            self.assertIn("failure_class=capacity", row)
            self.assertIn(f"capacity_log={server_log}", row)

    def test_an_already_closed_owner_is_settled_after_the_group_drains(self):
        # 2026-09-17: the supervisor closed its owner row (completed-supervisor)
        # while alive, so the in-process settlement deferred and no delivery
        # record existed. After the drain this watcher must retry it.
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            jobs = base / "jobs.log"
            attempt = "att-closed-owner"
            worker = subprocess.Popen(
                ["sleep", "0.08"],
                env={**os.environ, D.ATTEMPT_DESCENDANT_ENV: attempt},
                start_new_session=True,
            )
            identity = D.process_launch_identity(worker.pid)
            metadata = ",".join(
                f"{key}={value}"
                for key, value in {
                    **identity,
                    "attempt_id": attempt,
                    "launch_lifecycle": "detached",
                    "log_file": str(base / "owner.jsonl"),
                    "parent_attempt_id": "-",
                    "parent_completion_delivery": "claude-parent-runtime",
                    "parent_sid": "sess-closed-owner",
                    "owner_route_id": "rt-closed-owner",
                    "harness": "claude",
                }.items()
            )
            jobs.write_text(
                "2026-09-17T00:00:00Z\topen\t/repo\t/wt\towner\t"
                f"{OWNER},{metadata}\n",
                encoding="utf-8",
            )
            self.assertTrue(D.close_attempt_row(jobs, attempt, "completed-supervisor"))
            records = base / "pending-delivery"
            self.assertFalse(any(records.rglob("*.json")) if records.exists() else False)
            watcher = subprocess.Popen(
                [
                    sys.executable,
                    str(WATCH),
                    "--jobs", str(jobs),
                    "--attempt-id", attempt,
                    "--pid", str(worker.pid),
                    "--pid-start", identity["pid_start"],
                    "--pgid", identity["pgid"],
                    "--interval", "0.02",
                ],
            )
            worker.wait(timeout=5)
            self.assertEqual(watcher.wait(timeout=10), 0)
            made = list(records.rglob("*.json"))
            self.assertEqual(len(made), 1)
            record = json.loads(made[0].read_text(encoding="utf-8"))
            self.assertEqual(record["route_id"], "rt-closed-owner")
            self.assertEqual(record["state"], "pending")

    def test_ledger_failure_does_not_change_missing_result_close(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            jobs = base / "jobs.log"
            attempt = "att-degradation-write-failure"
            worker = subprocess.Popen(
                ["sleep", "0.08"],
                env={**os.environ, D.ATTEMPT_DESCENDANT_ENV: attempt},
                start_new_session=True,
            )
            identity = D.process_launch_identity(worker.pid)
            metadata = ",".join(
                f"{key}={value}"
                for key, value in {
                    **identity,
                    "attempt_id": attempt,
                    "launch_lifecycle": "detached",
                    "log_file": str(base / "missing.jsonl"),
                    "route_id": "rt-ledger-failure",
                    "route_hash": "sha256:ledger-failure",
                    "route_node": "test-alternative",
                    "parallel_group": "test",
                    "batch_declared_size": "2",
                    "batch_parallel_leg_index": "1",
                }.items()
            )
            jobs.write_text(
                "2026-08-09T00:00:00Z\topen\t/repo\t/wt\tworker\t"
                f"{CURRENT},{metadata}\n",
                encoding="utf-8",
            )
            (base / "degradations").write_text("not-a-directory", encoding="utf-8")
            watcher = subprocess.Popen(
                [
                    sys.executable,
                    str(WATCH),
                    "--jobs", str(jobs),
                    "--attempt-id", attempt,
                    "--pid", str(worker.pid),
                    "--pid-start", identity["pid_start"],
                    "--pgid", identity["pgid"],
                    "--interval", "0.02",
                ]
            )
            worker.wait(timeout=5)
            self.assertEqual(watcher.wait(timeout=5), 0)
            row = jobs.read_text(encoding="utf-8")
            self.assertIn("\tdone\t", row)
            self.assertIn("note=dead-missing-result", row)
            self.assertIn("terminal_event=dispatch-reap-missing-result", row)
            self.assertEqual(
                (base / "degradations").read_text(encoding="utf-8"),
                "not-a-directory",
            )

    def test_live_parent_defers_missing_result_close(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            parent = subprocess.Popen(["sleep", "5"])
            leg = subprocess.Popen(
                ["sleep", "0.08"],
                env={**os.environ, D.ATTEMPT_DESCENDANT_ENV: "att-leg"},
                start_new_session=True,
            )
            try:
                jobs = _registry_with_parent_and_leg(base, parent, leg)
                leg_identity = D.process_launch_identity(leg.pid)
                watcher = subprocess.Popen(
                    [
                        sys.executable,
                        str(WATCH),
                        "--jobs", str(jobs),
                        "--attempt-id", "att-leg",
                        "--pid", str(leg.pid),
                        "--pid-start", leg_identity["pid_start"],
                        "--pgid", leg_identity["pgid"],
                        "--interval", "0.02",
                        "--parent-recheck-interval", "0.05",
                    ]
                )
                leg.wait(timeout=5)
                deadline = time.time() + 3
                row = ""
                while time.time() < deadline:
                    row = jobs.read_text(encoding="utf-8")
                    if "reap_close_deferred=parent-live:process" in row:
                        break
                    time.sleep(0.05)
                self.assertIn("reap_close_deferred=parent-live:process", row)
                self.assertIn("att-leg", row.splitlines()[1])
                self.assertEqual(row.splitlines()[1].split("\t")[1], "open")

                self.assertTrue(
                    D.close_attempt_row(jobs, "att-leg", "completed-marker")
                )
                self.assertEqual(watcher.wait(timeout=5), 0)
                row = jobs.read_text(encoding="utf-8")
                leg_row = next(
                    line for line in row.splitlines() if "att-leg" in line
                )
                self.assertEqual(leg_row.split("\t")[1], "done")
                self.assertIn("note=completed-marker", leg_row)
                self.assertNotIn("dead-missing-result", leg_row)
            finally:
                parent.kill()
                parent.wait(timeout=5)

    def test_dead_parent_closes_leg_as_missing_result(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            parent = subprocess.Popen(["sleep", "5"])
            leg = subprocess.Popen(
                ["sleep", "0.08"],
                env={**os.environ, D.ATTEMPT_DESCENDANT_ENV: "att-leg"},
                start_new_session=True,
            )
            jobs = _registry_with_parent_and_leg(base, parent, leg)
            leg_identity = D.process_launch_identity(leg.pid)
            leg.wait(timeout=5)
            parent.kill()
            parent.wait(timeout=5)
            watcher = subprocess.Popen(
                [
                    sys.executable,
                    str(WATCH),
                    "--jobs", str(jobs),
                    "--attempt-id", "att-leg",
                    "--pid", str(leg.pid),
                    "--pid-start", leg_identity["pid_start"],
                    "--pgid", leg_identity["pgid"],
                    "--interval", "0.02",
                    "--parent-recheck-interval", "0.05",
                ]
            )
            self.assertEqual(watcher.wait(timeout=5), 0)
            row = jobs.read_text(encoding="utf-8")
            leg_row = next(line for line in row.splitlines() if "att-leg" in line)
            self.assertEqual(leg_row.split("\t")[1], "done")
            self.assertIn("note=dead-missing-result", leg_row)
            self.assertIn("terminal_event=dispatch-reap-missing-result", leg_row)

    def test_foreign_or_terminal_parent_row_is_not_a_completion_window(self):
        cases = {
            "terminal-parent": dict(parent_open=False),
            "foreign-worktree": dict(foreign="worktree"),
            "foreign-pid-start": dict(foreign="pid_start"),
        }
        for name, kwargs in cases.items():
            with self.subTest(name):
                with tempfile.TemporaryDirectory() as td:
                    base = Path(td)
                    parent = subprocess.Popen(["sleep", "5"])
                    leg = subprocess.Popen(
                        ["sleep", "0.08"],
                        env={**os.environ, D.ATTEMPT_DESCENDANT_ENV: "att-leg"},
                        start_new_session=True,
                    )
                    try:
                        jobs = _registry_with_parent_and_leg(base, parent, leg, **kwargs)
                        leg_identity = D.process_launch_identity(leg.pid)
                        watcher = subprocess.Popen(
                            [
                                sys.executable,
                                str(WATCH),
                                "--jobs", str(jobs),
                                "--attempt-id", "att-leg",
                                "--pid", str(leg.pid),
                                "--pid-start", leg_identity["pid_start"],
                                "--pgid", leg_identity["pgid"],
                                "--interval", "0.02",
                                "--parent-recheck-interval", "0.05",
                            ]
                        )
                        leg.wait(timeout=5)
                        self.assertEqual(watcher.wait(timeout=5), 0)
                        row = jobs.read_text(encoding="utf-8")
                        leg_row = next(
                            line for line in row.splitlines() if "att-leg" in line
                        )
                        self.assertEqual(leg_row.split("\t")[1], "done")
                        self.assertIn("note=dead-missing-result", leg_row)
                        self.assertNotIn("reap_close_deferred=parent-live", leg_row)
                    finally:
                        parent.kill()
                        parent.wait(timeout=5)


class DrainScanRetryTest(unittest.TestCase):
    """One unverifiable drain scan (a process exiting mid-walk) is looked at
    again within the drain grace; only a lasting one leaves no receipt."""

    def setUp(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("dispatch_reap_watch_module", WATCH)
        self.watcher = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.watcher)

    def watch(self, scans, grace, metadata=None):
        from contextlib import ExitStack
        from types import SimpleNamespace
        metadata = metadata or {"attempt_id": "att-drain-retry", "pid_observer_ns": "pid:[1]"}
        annotated = []
        patches = {
            "attempt_record": lambda jobs, aid: (["now", "open", "/r", "/w", "s", ""], metadata),
            "exact_binding": lambda *args: True,
            "process_namespace_identity": lambda *args: metadata.get("pid_observer_ns", "pid:[1]"),
            "attempt_scan_namespace_authority": lambda meta: True,
            "launched_attempt_identity": lambda fields: "identity",
            "process_start_ticks": lambda pid: "gone",
            "process_group_observation": lambda pgid: D.ProcessGroupObservation("empty"),
            "attempt_tagged_descendants": (mock.Mock(side_effect=scans) if scans is not None
                                           else D.attempt_tagged_descendants),
            # Stop right after the receipt write: the rest of the watcher is not under test.
            "annotate_attempt_row_if": lambda jobs, aid, values, predicate, **kwargs: annotated.append(values) and False,
        }
        args = SimpleNamespace(jobs=Path("/nonexistent/jobs.log"), attempt_id="att-drain-retry", pid=1,
                               pid_start="1", pgid=1, interval=0.01, drain_interval_max=0.02,
                               residue_grace=grace, parent_recheck_interval=1.0)
        with ExitStack() as stack:
            for name, value in patches.items():
                stack.enter_context(mock.patch.object(self.watcher, name, value))
            stack.enter_context(mock.patch.object(self.watcher.time, "sleep", lambda seconds: None))
            return self.watcher.watch(args), annotated

    def test_a_transient_unverifiable_scan_still_seals_the_drain_receipt(self):
        denied = D.ProcessGroupObservation("unverifiable", reason="procfs-environ:4242:same-uid-unobservable")
        code, annotated = self.watch([denied, D.ProcessGroupObservation("empty")], grace=30.0)
        self.assertEqual(code, 65)   # the stubbed receipt write reports no change
        self.assertEqual(len(annotated), 1)
        self.assertEqual(annotated[0]["attempt_descendant_proof"], D.ATTEMPT_DESCENDANT_PROOF)
        self.assertEqual(annotated[0]["launch_outcome"], "governed-process-group-drained")

    def test_a_lasting_unverifiable_scan_leaves_no_receipt(self):
        denied = D.ProcessGroupObservation("unverifiable", reason="procfs-environ:4242:same-uid-unobservable")
        code, annotated = self.watch([denied], grace=0.0)
        self.assertEqual((code, annotated), (69, []))

    def test_an_unreadable_session_across_a_uid_boundary_lets_the_drain_seal(self):
        """A process the attempt cannot have made (forked by a live parent under another UID, e.g.
        root's `sshd [priv]` for a VS Code Remote-SSH session) does not hold an ended attempt open."""
        import errno
        import os
        import subprocess
        import sys
        ended = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True)
        metadata = dict(D.process_launch_identity(ended.pid), pgid=str(ended.pid),
                        attempt_id="att-foreign-session", launch_lifecycle="detached")
        ended.terminate()
        ended.wait(timeout=5)
        late = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        self.addCleanup(late.wait, 5)
        self.addCleanup(late.terminate)
        read_bytes, read_text = Path.read_bytes, Path.read_text

        def denied(path):
            if str(path) == f"/proc/{late.pid}/environ":
                raise PermissionError(errno.EACCES, "denied")
            return read_bytes(path)

        def parent_as(uid):
            def text(path, *args, **kwargs):
                value = read_text(path, *args, **kwargs)
                if str(path) == f"/proc/{os.getpid()}/status":
                    value = "".join(f"Uid:\t{uid}\t{uid}\t{uid}\t{uid}\n" if line.startswith("Uid:") else line + "\n"
                                    for line in value.splitlines())
                return value
            return text
        with mock.patch.object(Path, "read_bytes", denied), mock.patch.object(Path, "read_text", parent_as(0)):
            code, annotated = self.watch(None, grace=0.0, metadata=metadata)
        self.assertEqual(code, 65)   # the stubbed receipt write reports no change
        self.assertEqual(annotated[0]["launch_outcome"], "governed-process-group-drained")
        with mock.patch.object(Path, "read_bytes", denied), \
                mock.patch.object(Path, "read_text", parent_as(os.getuid())):
            self.assertEqual(self.watch(None, grace=0.0, metadata=metadata), (69, []))   # could be the attempt's own


class WorkerHomeRetentionCarrierTest(unittest.TestCase):
    """The post-exit reaper retires expired homes; no user step is involved."""

    def seed_expired_home(self, base, attempt):
        from worker_runtime_home import _builder
        key = _builder.worker_home_key(str(ROOT), "claude", "stage", attempt)
        home = base / "homes" / "workers" / key
        home.mkdir(parents=True)
        (home / "worker-home.json").write_text(json.dumps({
            "harness": "claude", "worker_type": "stage", "source": str(ROOT), "profile": None,
            "schema": 2, "identity": attempt, "key": key, "destination": False}))
        (home / "projects").mkdir()
        (home / "projects" / "t.jsonl").write_text("x")
        old = time.time() - 30 * 86400
        for path in (home / "projects" / "t.jsonl", home / "projects", home / "worker-home.json", home):
            os.utime(path, (old, old))
        # An earlier pass proved it eligible long ago.
        state = base / "homes" / "worker-retention"
        state.mkdir(parents=True)
        (state / "state.json").write_text(json.dumps({
            "schema": 1, "last_pass_ns": 0, "cursor": "",
            "eligible_since": {key: time.time_ns() - 20 * 86400 * 10**9}}))
        return home, key

    def test_a_reaper_exit_retires_an_expired_home_and_keeps_the_rest(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td).resolve()
            jobs = base / "jobs.log"
            attempt = "att-reaper-carrier"
            worker = subprocess.Popen(
                ["sleep", "0.05"], env={**os.environ, D.ATTEMPT_DESCENDANT_ENV: attempt},
                start_new_session=True)
            identity = D.process_launch_identity(worker.pid)
            metadata = ",".join(f"{k}={v}" for k, v in {
                **identity, "attempt_id": attempt, "launch_lifecycle": "detached",
                "log_file": str(base / "missing.jsonl")}.items())
            expired, key = self.seed_expired_home(base, "att-long-gone")
            from worker_runtime_home import _builder
            live_key = _builder.worker_home_key(str(ROOT), "claude", "stage", attempt)
            live_home = base / "homes" / "workers" / live_key
            live_home.mkdir(parents=True)
            (live_home / "worker-home.json").write_text(json.dumps({
                "harness": "claude", "worker_type": "stage", "source": str(ROOT), "profile": None,
                "schema": 2, "identity": attempt, "key": live_key, "destination": False}))
            jobs.write_text(
                "2026-10-01T00:00:00Z\topen\t/repo\t/wt\tworker\t"
                f"{CURRENT},{metadata},runtime_home={live_home}\n"
                "2026-09-01T00:00:00Z\tdone\t/repo\t/wt\told\t"
                f"{CURRENT},attempt_id=att-long-gone,harness=claude,runtime_home={expired},"
                "launch_outcome=never-launched\n", encoding="utf-8")
            watcher = subprocess.Popen([
                sys.executable, str(WATCH), "--jobs", str(jobs), "--attempt-id", attempt,
                "--pid", str(worker.pid), "--pid-start", identity["pid_start"],
                "--pgid", identity["pgid"], "--interval", "0.02"])
            worker.wait(timeout=5)
            self.assertEqual(watcher.wait(timeout=30), 0)
            self.assertFalse(expired.exists())
            self.assertTrue(live_home.is_dir())
            self.assertIn("\tdone\t", jobs.read_text(encoding="utf-8").splitlines()[0])
            record = json.loads((base / "homes/worker-retention/state.json").read_text())
            self.assertEqual(record["last_result"]["removed"], 1)
            # The attempt this reaper just closed carries a real post-exit receipt, so
            # its own home is proven quiet and starts its window instead of being
            # held as unverifiable for ever.
            self.assertEqual(record["last_result"]["kept"], {"within-window": 1})
            self.assertEqual(record["last_result"]["unknown"], {})

    def test_a_failing_sweep_does_not_change_the_watcher_result(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("dispatch_reap_watch_main", WATCH)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        import worker_home_retention
        with mock.patch.object(module, "watch", return_value=65), \
                mock.patch.object(worker_home_retention, "sweep", side_effect=RuntimeError("boom")):
            code = module.main(["--jobs", "/nonexistent/jobs.log", "--attempt-id", "att-x",
                                "--pid", "1", "--pid-start", "1", "--pgid", "1"])
        self.assertEqual(code, 65)
        with mock.patch.object(module, "watch", return_value=0), \
                mock.patch.object(worker_home_retention, "sweep") as sweep:
            self.assertEqual(module.main(["--jobs", "/nonexistent/jobs.log", "--attempt-id", "att-x",
                                          "--pid", "1", "--pid-start", "1", "--pgid", "1"]), 0)
        sweep.assert_called_once()


class DeliveryPruneCarrierTest(unittest.TestCase):
    """The reaper's home pass carries the one bounded seven-day terminal prune."""

    def record(self, base, suffix, state, age_days):
        import dispatch_pending_delivery as P
        receipt = {"schema_version": 2, "state": "delivered", "kind": "fixture",
                   "parent_attempt_id": "att-" + "a" * 32, "job_registry": str(base / "jobs.log"),
                   "children": [], "delivery_classification": "attention"}
        delivery_id = "delivery-" + suffix * 32
        P.create(base, recipient_kind="claude-parent-runtime", recipient_key="sess-reaper",
                 delivery_id=delivery_id, session_generation="", session_generation_supported="0",
                 attempt_ids=["att-" + suffix * 32], parent_attempt_id="att-" + "a" * 32,
                 route_id="rt-reaper", route_node="execute", receipt=receipt,
                 receipt_digest=P._canonical_receipt_digest(receipt),
                 row_revisions={"att-" + suffix * 32: "deadbeef"})
        if state == "acked":
            P.claim(base, "sess-reaper", delivery_id, claim_owner="fixture", lease_seconds=60)
            P.ack(base, "sess-reaper", delivery_id, acked_by="fixture")
        path = P.record_path(base, "sess-reaper", delivery_id)
        old = time.time() - age_days * 86400
        os.utime(path, (old, old))
        return path

    def test_a_reaper_exit_prunes_week_old_terminal_deliveries(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td).resolve()
            jobs = base / "jobs.log"
            attempt = "att-reaper-prune"
            (base / "homes" / "workers").mkdir(parents=True, mode=0o700)
            old = self.record(base, "1", "acked", 8)
            recent = self.record(base, "2", "acked", 1)
            opened = self.record(base, "3", "pending", 30)
            worker = subprocess.Popen(
                ["sleep", "0.05"], env={**os.environ, D.ATTEMPT_DESCENDANT_ENV: attempt},
                start_new_session=True)
            identity = D.process_launch_identity(worker.pid)
            metadata = ",".join(f"{k}={v}" for k, v in {
                **identity, "attempt_id": attempt, "launch_lifecycle": "detached",
                "log_file": str(base / "missing.jsonl")}.items())
            jobs.write_text("2026-10-01T00:00:00Z\topen\t/repo\t/wt\tworker\t"
                            f"{CURRENT},{metadata}\n", encoding="utf-8")
            watcher = subprocess.Popen([
                sys.executable, str(WATCH), "--jobs", str(jobs), "--attempt-id", attempt,
                "--pid", str(worker.pid), "--pid-start", identity["pid_start"],
                "--pgid", identity["pgid"], "--interval", "0.02"])
            worker.wait(timeout=5)
            self.assertEqual(watcher.wait(timeout=60), 0)
            self.assertFalse(old.exists())
            self.assertTrue(Path(str(old) + ".pruned").is_file())
            self.assertTrue(recent.is_file())
            self.assertTrue(opened.is_file())

    def watch_main(self, jobs):
        import importlib.util
        spec = importlib.util.spec_from_file_location("dispatch_reap_watch_prune", WATCH)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        import dispatch_pending_delivery as P
        real_bounded = P.prune_bounded
        with mock.patch.object(module, "watch", return_value=65), \
                mock.patch.object(P, "PRUNE_RECORD_LIMIT", 1), \
                mock.patch.object(P, "prune_bounded", side_effect=real_bounded) as bounded, \
                mock.patch.object(P, "prune", side_effect=AssertionError("unbounded prune")) as unbounded:
            code = module.main(["--jobs", str(jobs), "--attempt-id", "att-x",
                                "--pid", "1", "--pid-start", "1", "--pgid", "1"])
        return code, bounded, unbounded

    def test_one_reaper_exit_prunes_once_within_the_bound(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td).resolve()
            jobs = base / "jobs.log"
            jobs.write_text("", encoding="utf-8")
            (base / "homes" / "workers").mkdir(parents=True, mode=0o700)
            olds = [self.record(base, "1", "acked", 9), self.record(base, "2", "acked", 8)]
            code, bounded, unbounded = self.watch_main(jobs)
            self.assertEqual(code, 65)
            # One prune per pass, the bounded one, under its record limit.
            bounded.assert_called_once()
            unbounded.assert_not_called()
            self.assertEqual([p.exists() for p in olds], [False, True])
            # The next exit inside the pass interval is not due: no prune at all.
            code, bounded, unbounded = self.watch_main(jobs)
            self.assertEqual(code, 65)
            bounded.assert_not_called()
            unbounded.assert_not_called()
            self.assertTrue(olds[1].exists())

    def test_a_failing_prune_does_not_change_the_watcher_result(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("dispatch_reap_watch_prune_fail", WATCH)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        import dispatch_pending_delivery as P
        with tempfile.TemporaryDirectory() as td:
            base = Path(td).resolve()
            jobs = base / "jobs.log"
            jobs.write_text("", encoding="utf-8")
            (base / "homes" / "workers").mkdir(parents=True, mode=0o700)
            with mock.patch.object(module, "watch", return_value=65), \
                    mock.patch.object(P, "prune_bounded", side_effect=RuntimeError("boom")) as bounded:
                code = module.main(["--jobs", str(jobs), "--attempt-id", "att-x",
                                    "--pid", "1", "--pid-start", "1", "--pgid", "1"])
            self.assertEqual(code, 65)
            bounded.assert_called_once()


class AttemptRecordIndexTest(unittest.TestCase):
    """attempt_record reads the one row through the shared view, with the
    same last-match answer as the old whole-file text scan."""

    def setUp(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("dispatch_reap_watch_record", WATCH)
        self.watcher = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.watcher)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.jobs = Path(self.tmp.name) / "jobs.log"

    @staticmethod
    def _line(status, aid, note):
        return f"t\t{status}\t/repo\t/wt\towner\tattempt_id={aid},note={note}"

    def _check(self, raw, aid, expect_note):
        self.jobs.write_bytes(raw)
        found = self.watcher.attempt_record(self.jobs, aid)
        text = self.watcher._attempt_record_from_text(self.jobs, aid)
        self.assertEqual(found, text)
        if expect_note is None:
            self.assertIsNone(found)
        else:
            self.assertEqual(found[1]["note"], expect_note)

    def test_last_row_wins_including_a_partial_tail_row(self):
        rows = "\n".join([self._line("open", "att-a", "first"), self._line("open", "att-b", "other"),
                          self._line("done", "att-a", "second")])
        self._check((rows + "\n").encode(), "att-a", "second")
        self._check(rows.encode(), "att-a", "second")
        self._check((rows + "\n" + self._line("done", "att-a", "tail")).encode(), "att-a", "tail")
        self._check(rows.encode(), "att-missing", None)

    def test_missing_registry_and_noncanonical_separators(self):
        self.assertIsNone(self.watcher.attempt_record(self.jobs, "att-a"))
        raw = (self._line("open", "att-a", "one") + "\r\n" + self._line("done", "att-a", "two")
               + "\n").encode()
        self._check(raw, "att-a", "two")


if __name__ == "__main__":
    unittest.main()
