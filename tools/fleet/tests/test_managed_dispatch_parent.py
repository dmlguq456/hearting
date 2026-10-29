#!/usr/bin/env python3
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

TOOLS = Path(__file__).resolve().parents[2]
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

from fleet import fleet, render, model  # noqa: E402
from fleet.collectors import dispatch, resolve_parent_edges, _mark_dispatch_child_sessions  # noqa: E402
from fleet.model import DispatchJob, Session  # noqa: E402
from fleet.tests.snapshot_fixture import build_observed_lines


MANAGED = "/home/u/.codex/.harness/managed-sessions/session-live"


def flatten(lines):
    return "\n".join("".join(text for text, _key in line) for line in lines if line)


class ManagedDispatchParentTest(unittest.TestCase):
    def setUp(self):
        model.reset_parent_edge_tracker()

    def tearDown(self):
        model.reset_parent_edge_tracker()
        render.set_show_all(False)

    def session(self, sid="stale-visible-thread", pid=10, managed_dir=MANAGED):
        return Session(
            harness="codex", pid=pid, cwd="/work/repo", session_id=sid,
            slug="repo", title="managed parent", liveness="working",
            managed_dir=managed_dir,
        )

    def job(self, managed_dir=MANAGED, source="jobs"):
        return DispatchJob(
            key="code", slug="managed-owner", cwd="/work/repo-wt",
            parent_sid="current-thread-not-on-tui-row",
            parent_managed_dir=managed_dir,
            is_child=True, harness="codex", source=source,
            capability_mode="debug", qa="standard", liveness="working",
        )

    def rendered(self, sessions, job):
        return flatten(build_observed_lines(
            sessions, [job], "both", False, 0, layout="wide", term_width=180,
        ))

    def test_current_parent_and_harness_share_one_coherent_read_pass(self):
        from fleet.collectors import _effective_job_parent
        utilities = TOOLS.parent / "utilities"
        if str(utilities) not in sys.path:
            sys.path.insert(0, str(utilities))
        import dispatch_seat_handover as handover
        import session_tidy as tidy
        job = self.job()
        meta = {"parent_sid": job.parent_sid, "parent_harness": "codex", "attempt_id": "att-coherent",
                "dispatch_depth": "1", "worker_type": "owner", "route_id": "rt-coherent", "route_hash": "sha256:coherent"}
        job._registry_metadata, job._registry_path = meta, "/fixture/jobs.log"
        binding = handover.binding_of(job._registry_path, meta)
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(tidy, "state_root", return_value=Path(tmp)):
            seat = tidy.Seat("pane", "coherent")
            def publish(sid, harness, ts):
                tidy._append_ledger_entry(seat, {"event": "handover", "source": "retire",
                    "from": {"harness": "codex", "sid": job.parent_sid},
                    "sid": sid, "harness": harness, "bindings": [binding], "ts": ts})
            publish("current-B", "claude", 1)
            resolve = handover.resolve_recipient
            def moved(*args, **kwargs):
                answer = resolve(*args, **kwargs)
                publish("current-C", "opencode", 2)
                return answer
            with mock.patch.object(handover, "resolve_recipient", side_effect=moved) as reader:
                self.assertEqual(_effective_job_parent(job), ("current-B", "claude"))
                reader.assert_called_once()
            self.assertEqual(_effective_job_parent(job), ("current-C", "opencode"))

    def test_unreadable_lineage_retains_only_the_previously_confirmed_edge_for_existing_grace(self):
        from fleet.collectors import _effective_job_parent
        utilities = TOOLS.parent / "utilities"
        if str(utilities) not in sys.path:
            sys.path.insert(0, str(utilities))
        import dispatch_seat_handover as handover
        import session_tidy as tidy
        job = self.job()
        job._registry_metadata = {"parent_sid": job.parent_sid, "parent_harness": "codex",
            "attempt_id": "att-grace", "dispatch_depth": "1", "worker_type": "owner",
            "route_id": "rt-grace", "route_hash": "sha256:grace"}
        job._registry_path = "/fixture/jobs.log"
        parent = self.session(sid="confirmed-successor")
        original = job.parent_sid
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(tidy, "state_root", return_value=Path(tmp)):
            seat = tidy.Seat("pane", "grace")
            tidy._append_ledger_entry(seat, {"event": "succession", "source": "tidy-clear",
                "harness": "codex", "sid": parent.session_id, "from": original, "ts": 1})
            resolve_parent_edges([parent], [job])
            self.assertEqual(job._parent_edge_sid, parent.session_id)
            tidy._ledger_path(seat).write_text('{broken ledger\n')
            self.assertEqual(_effective_job_parent(job), ("", ""))
            for _ in range(3):
                resolve_parent_edges([self.session(sid=original)], [job])
                self.assertEqual(job._parent_edge_sid, parent.session_id)
            resolve_parent_edges([self.session(sid=original)], [job])
            self.assertIsNone(job._parent_edge_sid)
            model.reset_parent_edge_tracker()
            resolve_parent_edges([self.session(sid=original)], [job])
            self.assertIsNone(job._parent_edge_sid)
            self.assertFalse(job._parent_edge_promoted_orphan)
            classified = render._classify_group_jobs("repo", [job], [self.session(sid=original)])
            self.assertNotIn(job, classified["children"].get(original, []))
            self.assertIn(job, classified["orphans"])
        self.assertEqual(job.parent_sid, original)

    def test_unique_exact_managed_dir_recovers_parent(self):
        text = self.rendered([self.session()], self.job())
        self.assertIn("managed parent", text)
        self.assertIn("managed-owner", text)
        self.assertNotIn("orphaned dispatch rows", text)
        self.assertNotIn("(orphan)", text)

    def test_collector_hidden_current_thread_attaches_to_original_tui(self):
        for reverse in (False, True):
            with self.subTest(reverse=reverse):
                model.reset_parent_edge_tracker()
                tui = self.session()
                job = self.job()
                server = self.session(sid=job.parent_sid, pid=11)
                server.app_server = True
                server._managed_client_present = True
                sessions = [server, tui] if reverse else [tui, server]
                resolve_parent_edges(sessions, [job])
                self.assertEqual(job._parent_edge_sid, tui.session_id)
                self.assertFalse(job._parent_edge_promoted_orphan)
                self.assertEqual(job.parent_sid, "current-thread-not-on-tui-row")
                text = self.rendered(sessions, job)
                self.assertIn("managed-owner", text)
                self.assertNotIn("(orphan)", text)
                self.assertNotIn("orphaned dispatch rows", text)

    def test_same_thread_client_wins_over_hidden_server_in_either_order(self):
        for reverse in (False, True):
            job = self.job()
            tui = self.session(sid=job.parent_sid)
            server = self.session(sid=job.parent_sid, pid=11)
            server.app_server = True
            server._managed_client_present = True
            sessions = [server, tui] if reverse else [tui, server]
            resolve_parent_edges(sessions, [job])
            self.assertEqual(job._parent_edge_sid, tui.session_id)
            self.assertFalse(job._parent_edge_promoted_orphan)

    def test_managed_edge_grace_keeps_original_authority_and_visible_identity(self):
        tui, job = self.session(), self.job()
        resolve_parent_edges([tui], [job])
        for _ in range(3):
            resolve_parent_edges([], [job])
            self.assertEqual(job._parent_edge_sid, tui.session_id)
        resolve_parent_edges([], [job])
        self.assertTrue(job._parent_edge_promoted_orphan)

    def test_dead_exact_parent_is_not_replaced_by_other_live_thread(self):
        job = self.job()
        dead = self.session(sid=job.parent_sid, pid=11)
        dead.liveness = "dead"
        resolve_parent_edges([dead, self.session()], [job])
        self.assertTrue(job._parent_edge_promoted_orphan)

    def test_child_or_unknown_identity_cannot_become_managed_parent(self):
        for kind in ("child", "sidless"):
            with self.subTest(kind=kind):
                tui, job = self.session(), self.job()
                if kind == "child":
                    tui.is_child = True
                else:
                    tui.session_id = None
                resolve_parent_edges([tui], [job])
                self.assertTrue(job._parent_edge_promoted_orphan)

    def test_mismatched_or_ambiguous_managed_dir_stays_orphan(self):
        mismatch = self.rendered(
            [self.session(managed_dir=MANAGED + "-other")], self.job(),
        )
        self.assertIn("(orphan)", mismatch)

        ambiguous = self.rendered(
            [self.session(pid=10), self.session(sid="another", pid=11)], self.job(),
        )
        self.assertIn("(orphan)", ambiguous)

    def test_plugin_queue_does_not_use_managed_dir_fallback(self):
        text = self.rendered([self.session()], self.job(source="plugin-queue"))
        self.assertIn("(orphan)", text)

    def test_exact_parent_sid_remains_stronger_than_managed_dir(self):
        parent = self.session(sid="exact-parent", managed_dir=MANAGED + "-other")
        job = self.job(managed_dir=MANAGED)
        job.parent_sid = "exact-parent"
        text = self.rendered([parent], job)
        self.assertNotIn("(orphan)", text)

    def test_anonymous_root_is_not_hidden_by_a_cwd_only_dispatch_row(self):
        root = Session(harness="codex", pid=10, cwd="/work/repo", session_id=None,
                       slug="repo", liveness="working",
                       exec_child={"pid": 11, "comm": "node", "ownership_verified": True})
        job = DispatchJob(key="code", slug="worker", cwd="/work/repo", is_child=True,
                          harness="codex", source="jobs", liveness="working")
        _mark_dispatch_child_sessions([root], [job])
        self.assertFalse(root.is_child)
        self.assertIsNone(root.session_id)

    def test_resolved_root_parent_and_exec_job_render_once_under_exact_edge(self):
        root = Session(harness="codex", pid=10, cwd="/work/repo", session_id="root-sid",
                       slug="repo", title="unmanaged root", liveness="working",
                       exec_child={"pid": 11, "comm": "node", "ownership_verified": True})
        job = DispatchJob(key="code", slug="exec-worker", cwd="/work/repo-wt",
                          parent_sid="root-sid", parent_cwd="/work/repo", is_child=True,
                          harness="codex", source="jobs", liveness="working")
        _mark_dispatch_child_sessions([root], [job])
        resolve_parent_edges([root], [job])
        text = self.rendered([root], job)
        self.assertEqual(text.count("unmanaged root"), 1)
        self.assertIn("exec-worker", text)
        self.assertNotIn("(orphan)", text)

    def test_collect_cwd_resolver_exact_exec_ancestry_and_parent_card_join(self):
        """The root option, root rollout, owned exec chain and parent edge join once."""
        from fleet.collectors import codex, procscan
        from fleet.collectors.procscan import codex_effective_cwd
        import uuid

        with tempfile.TemporaryDirectory() as td:
            launch = str(Path(td) / "shell")
            target = str(Path(td) / "project")
            Path(launch).mkdir()
            Path(target).mkdir()
            effective = codex_effective_cwd(["codex", "--cd", target], launch)
            sid = str(uuid.uuid4())
            rollout = str(Path(td) / "codex" / "sessions" / "2026" / "10" / "03" /
                          ("rollout-2026-10-03T00-00-00-000Z-" + sid + ".jsonl"))
            tree = {4242: (1, 3600, "codex"), 4244: (4242, 170, "node")}
            identities = {4242: (1, "200"), 4244: (4242, "202")}
            with mock.patch.object(procscan, "_exec_identity", side_effect=identities.get), \
                 mock.patch.object(procscan, "read_environ",
                                   return_value={"AGENT_DISPATCH_ATTEMPT_ID": "att-fleet-fixture"}), \
                 mock.patch.object(procscan, "_exec_is_wrapper", return_value=False):
                owned = procscan.exec_child(4242, tree, procscan.children_index(tree),
                                            min_age=0, expected_start="200",
                                            attempt_id="att-fleet-fixture")
            self.assertTrue(owned["ownership_verified"])
            self.assertEqual([row["pid"] for row in owned["ancestry"]], [4242, 4244])

            readlink = os.readlink
            with mock.patch.object(codex.os, "readlink",
                                   side_effect=lambda path: effective if path == "/proc/4242/cwd" else readlink(path)), \
                 mock.patch.object(procscan, "_read_argv", return_value=["codex", "--cd", target]), \
                 mock.patch.object(procscan, "read_environ", return_value={"PWD": launch}), \
                 mock.patch.object(procscan, "is_shared_codex_daemon", return_value=False), \
                 mock.patch.object(codex, "_proc_rollout",
                                   side_effect=lambda pid, cwd, home: rollout if cwd == effective else None), \
                 mock.patch.object(codex, "_home", return_value=str(Path(td) / "codex")):
                resolved_sid = codex.session_id_of_process(4242, lambda: [])
            self.assertEqual(resolved_sid, sid)

            root = Session(harness="codex", pid=4242, cwd=effective,
                           session_id=resolved_sid, slug="project", title="unmanaged root",
                           liveness="working", exec_child=owned)
            job = DispatchJob(key="code", slug="exec-worker", cwd=str(Path(td) / "worker"),
                              parent_sid=resolved_sid, parent_cwd=launch, is_child=True,
                              harness="codex", source="jobs", liveness="working")
            _mark_dispatch_child_sessions([root], [job])
            resolve_parent_edges([root], [job])
            text = self.rendered([root], job)
            self.assertEqual(job._parent_edge_sid, sid)
            self.assertFalse(job._parent_edge_promoted_orphan)
            self.assertEqual(text.count("unmanaged root"), 1)
            self.assertIn("exec-worker", text)
            self.assertNotIn("(orphan)", text)

    def test_registry_sidecar_path_is_normalized_and_preserved_in_json(self):
        sidecar = MANAGED + "/managed-sidecars/batch.jsonl"
        with tempfile.TemporaryDirectory() as td:
            jobs_path = os.path.join(td, "jobs.log")
            pipe = (
                "capability=autopilot-code,capability_mode=debug,qa=standard,"
                "harness=codex,parent_sid=current,parent_cwd=/work/repo,"
                "managed_sidecar_log=" + sidecar
            )
            with open(jobs_path, "w", encoding="utf-8") as handle:
                handle.write(
                    "2026-08-10T00:00:00+00:00\topen\trepo\t/work/repo-wt\t"
                    "managed-owner\t" + pipe + "\n"
                )
            jobs, malformed = dispatch._scan_jobs_log(jobs_path, set())

        self.assertEqual(malformed, 0)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].parent_managed_dir, MANAGED)
        payload = json.loads(fleet._snapshot_json([], jobs, []))
        self.assertEqual(payload["jobs"][0]["parent_managed_dir"], MANAGED)

    def test_malformed_sidecar_paths_fail_closed(self):
        self.assertIsNone(dispatch._managed_parent_dir("relative/managed-sidecars/x.jsonl"))
        self.assertIsNone(dispatch._managed_parent_dir(
            "/home/u/managed-sessions/session-live/managed-sidecars/x.jsonl"
        ))
        self.assertIsNone(dispatch._managed_parent_dir(
            MANAGED + "/wrong-dir/x.jsonl"
        ))


if __name__ == "__main__":
    unittest.main()
