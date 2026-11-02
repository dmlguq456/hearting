"""Resource execution must remain visible while its exact model owner parks."""
import copy
import json
import sys
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from fleet import fleet, projection, render, route
from fleet.model import DispatchJob, ResourceJob, Session


def flatten(lines):
    return "\n".join("".join(text for text, _key in line) for line in lines if line)


class OwnerResourceChildrenTest(unittest.TestCase):
    def setUp(self):
        self.record = {
            "schema_version": 2, "cwd": "/work/project", "artifact_root": "/work/project/.agent_reports",
            "capability": "autopilot-lab", "capability_mode": "eval",
            "effective_intensity": "standard", "nodes": [
                {"id": "eval-smoke", "depends_on": []},
                {"id": "eval-run", "depends_on": ["eval-smoke"]},
                {"id": "report", "depends_on": ["eval-run"]},
            ],
        }
        self.record["route_hash"] = route.route_hash(self.record)
        self.record["route_id"] = "rt-" + self.record["route_hash"].split(":", 1)[1][:16]
        self.rid = self.record["route_id"]
        self.owner = DispatchJob(
            key="lab", slug="same-slug-owner", cwd="/work/project", pid=40,
            harness="claude", model="opus", liveness="idle", status="open",
            depth=1, dispatch_depth=1, worker_type="owner", assigned_contract="autopilot-lab",
            intensity="standard", attempt_id="att-owner-current", elapsed_min=700,
            owner_route_file="/routes/lab.json", owner_route_id=self.rid,
            owner_route_hash=self.record["route_hash"],
            summary="eval-smoke old model summary", summary_ts=100,
            state_evidence={"inputs": {"observed_liveness": {"state": "parked-supervised"}}},
        )
        self.resource = ResourceJob(
            run_id="declared-run", cwd="/work/project", node="eval-run", route="/routes/lab.json",
            parent_attempt_id=self.owner.attempt_id, elapsed_min=410, liveness="working",
            registry_status="running", pid=41, starttime="123", command_hash="a" * 64,
        )
        self.evidence = {self.rid: {"eval-smoke": {"status": "done"}}}
        self.render_patch = mock.patch.object(render, "_COMPUTE_HOSTS", None)
        self.render_patch.start()
        render._ROUTE_FOLD.clear()

    def tearDown(self):
        self.render_patch.stop()
        render.set_process_view(False)
        render.set_show_all(False)
        render._ROUTE_FOLD.clear()

    def attach(self, jobs=None, resources=None, records=None):
        projection.attach_projections(
            [], jobs if jobs is not None else [self.owner],
            resources=resources if resources is not None else [self.resource],
            route_records=records if records is not None else {self.rid: self.record},
            node_evidence=self.evidence, spec_markers={}, capability_groundings={}, now=1000,
        )

    def test_legacy_resource_lights_current_node_for_every_harness_without_rewriting_summary(self):
        original = copy.deepcopy(self.record)
        for harness in ("claude", "codex", "opencode"):
            with self.subTest(harness=harness):
                self.owner.harness = harness
                self.attach()
                work = self.owner.work_projection
                self.assertEqual((work.route_node, work.stage_label, work.node_state),
                                 ("eval-run", "eval-run", "active"))
                self.assertEqual([n.id for n in work.active_nodes], ["eval-run"])
                self.assertEqual(self.owner.resource_wait,
                                 {"state": "resource-parked", "nodes": ["eval-run"],
                                  "run_ids": ["declared-run"]})
                self.assertEqual(self.owner.summary, "eval-smoke old model summary")
                self.assertEqual(self.owner.summary_ts, 100)
                self.assertEqual(work.progress.done, 1)
        self.assertEqual(original, self.record)

    def test_same_cwd_and_slug_cannot_attach_to_a_different_attempt(self):
        retry = replace(self.owner, attempt_id="att-other", state_evidence={})
        self.attach(jobs=[retry, self.owner])
        self.assertEqual(retry.resource_children, [])
        self.assertEqual(self.owner.resource_children, [self.resource])
        self.assertIsNone(retry.resource_wait)
        # A route is shared by retries, but the parked activity belongs to its exact owner only.
        self.assertIsNotNone(self.owner.resource_wait)

    def test_parked_owner_active_resource_uses_the_shared_manual_blink_phase(self):
        self.attach()
        phases = []
        for phase in (True, False):
            with mock.patch.object(render, "_BLINK_ON", phase):
                lines = render._build_lines([], [self.owner], "both", False, 0,
                                            layout="wide", term_width=180, resources=[self.resource])
                phases.append([key for line in lines if line for text, key in line
                               if text == "eval-run" and key and key.startswith("stg")])
        self.assertTrue(phases[0] and phases[1])
        self.assertNotEqual(phases[0], phases[1])
        self.assertEqual(self.owner.liveness, "idle")

    def test_duplicate_exact_parent_rows_and_missing_parent_never_guess(self):
        duplicate = replace(self.owner)
        self.attach(jobs=[self.owner, duplicate])
        self.assertFalse(self.owner.resource_children)
        self.assertFalse(duplicate.resource_children)
        self.attach(resources=[replace(self.resource, parent_attempt_id=None)])
        self.assertFalse(self.owner.resource_children)

    def test_explicit_route_conflicts_keep_child_visible_but_do_not_claim_current_stage(self):
        for changes in ({"route": "/routes/other.json"}, {"route_id": "rt-other"},
                        {"route_hash": "sha256:foreign"}, {"route_node": "report"},
                        {"node": "unknown-node"}):
            with self.subTest(changes=changes):
                child = replace(self.resource, **changes)
                self.attach(resources=[child])
                self.assertEqual(self.owner.resource_children, [child])
                self.assertIsNone(self.owner.work_projection.route_node)
                self.assertIsNone(self.owner.resource_wait)

    def test_invalid_owner_binding_keeps_observed_child_without_overriding_unknown(self):
        self.attach(records={self.rid: {**self.record, "route_hash": "sha256:foreign"}})
        self.assertEqual(self.owner.resource_children, [self.resource])
        self.assertTrue(self.owner.work_projection.ambiguity)
        self.assertIsNone(self.owner.resource_wait)

    def test_invalid_other_owner_cannot_light_the_valid_owners_route(self):
        invalid = replace(self.owner, attempt_id="att-invalid", owner_route_hash="sha256:foreign")
        child = replace(self.resource, parent_attempt_id=invalid.attempt_id)
        self.attach(jobs=[invalid, self.owner], resources=[child])
        self.assertTrue(invalid.work_projection.ambiguity)
        self.assertIsNone(self.owner.work_projection.route_node)
        self.assertIsNone(self.owner.resource_wait)

    def test_explicit_modern_resource_tuple_uses_the_same_projection(self):
        child = replace(self.resource, route=None, node=None, route_file=self.resource.route,
                        route_id=self.rid, route_hash=self.record["route_hash"], route_node="eval-run")
        self.attach(resources=[child])
        self.assertEqual(self.owner.work_projection.route_node, "eval-run")
        self.assertEqual(self.owner.resource_wait["nodes"], ["eval-run"])

    def test_terminal_or_unobservable_resources_do_not_keep_owner_active(self):
        for state in ("exited", "stale"):
            with self.subTest(state=state):
                self.attach(resources=[replace(self.resource, liveness=state)])
                self.assertEqual(self.owner.resource_children[0].liveness, state)
                self.assertIsNone(self.owner.work_projection.route_node)
                self.assertIsNone(self.owner.resource_wait)
                self.assertEqual(render._resource_child_rows(self.owner), [])
                render.set_show_all(True)
                self.assertIn(state, flatten(render._resource_child_rows(self.owner)))
                render.set_show_all(False)
        self.attach(resources=[])
        self.assertEqual(self.owner.resource_children, [])

    def test_resource_dot_reuses_gpu_blink_and_observed_host_pid_without_log_age(self):
        self.attach()
        self.resource.local_placement = {"hostname": "mgpu-a1.example.test", "processes": [
            {"pid": self.resource.pid, "starttime": self.resource.starttime}]}
        self.resource.log_updated_at = 1
        for phase, key in ((True, "g_work"), (False, "g_work_off")):
            with self.subTest(phase=phase), mock.patch.object(render, "_BLINK_ON", phase):
                rows = render._resource_child_rows(self.owner, term_width=168)
                self.assertIn(("●", key), rows[0])
                self.assertIn("mgpu-a1.example.test", flatten(rows))
                self.assertIn("PID 41", flatten(rows))
                self.assertNotIn("ago", flatten(rows))
        for width in (24, 36, 60, 80, 168):
            rows = render._resource_child_rows(self.owner, term_width=width)
            self.assertLessEqual(sum(render._dw(t) for t, _ in rows[0]), width)
            if width >= 40:
                self.assertIn("eval run", flatten(rows))
                self.assertIn("6h 50m", flatten(rows))
        self.resource.local_placement = None
        self.assertNotIn("PID", flatten(render._resource_child_rows(self.owner, term_width=168)))
        self.resource.state_evidence = {"reason": "exact-identity-match", "current_identity": {
            "pid": self.resource.pid, "starttime": self.resource.starttime}}
        self.assertEqual(render._resource_process_location(self.resource), "PID 41")
        self.resource.state_evidence["current_identity"]["starttime"] = "456"
        self.assertEqual(render._resource_process_location(self.resource), "")

    def test_working_owner_and_plain_idle_owner_are_not_labeled_parked(self):
        for liveness, evidence in (("working", self.owner.state_evidence), ("idle", {})):
            with self.subTest(liveness=liveness):
                self.owner.liveness, self.owner.state_evidence = liveness, evidence
                self.attach()
                self.assertEqual(self.owner.work_projection.route_node, "eval-run")
                self.assertIsNone(self.owner.resource_wait)

    def test_parallel_resources_keep_all_nodes_in_sealed_order(self):
        record = copy.deepcopy(self.record)
        record["nodes"].insert(2, {"id": "eval-alt", "depends_on": ["eval-smoke"]})
        record["route_hash"] = route.route_hash(record)
        record["route_id"] = "rt-" + record["route_hash"].split(":", 1)[1][:16]
        self.owner.owner_route_id = record["route_id"]
        self.owner.owner_route_hash = record["route_hash"]
        second = replace(self.resource, run_id="alt-run", node="eval-alt")
        self.attach(resources=[second, self.resource], records={record["route_id"]: record})
        self.assertEqual(self.owner.resource_wait["nodes"], ["eval-run", "eval-alt"])
        self.assertEqual([n.id for n in self.owner.work_projection.active_nodes],
                         ["eval-run", "eval-alt"])

    def test_group_process_and_json_show_exact_child_and_current_wait(self):
        self.attach()
        for process in (False, True):
            render.set_process_view(process)
            for layout, width in (("wide", 168), ("narrow", 80), ("stack", 60)):
                with self.subTest(process=process, layout=layout):
                    lines = render._build_lines([], [self.owner], "both", width < 80, 0,
                                                layout=layout, term_width=width,
                                                resources=[self.resource], governor=None)
                    text = flatten(lines)
                    self.assertIn("resource eval run", text)
                    self.assertIn("6h 50m", text)
                    self.assertNotIn("old model summary", text)
                    self.assertEqual(text.count("resource eval run"), 1)
                    child_line = next(line for line in lines if line and "resource eval run" in flatten([line]))
                    self.assertLessEqual(sum(render._dw(t) for t, _ in child_line), width)
                    if width == 168:
                        self.assertIn('no log', text)
        snap = json.loads(fleet._snapshot_json([], [self.owner], [self.resource]))
        self.assertEqual(len(snap["jobs"]), 1)
        job = snap["jobs"][0]
        self.assertEqual(job["resource_children"][0]["run_id"], "declared-run")
        self.assertEqual(job["resource_wait"]["state"], "resource-parked")
        self.assertEqual(job["summary"], "eval-smoke old model summary")
        self.assertEqual(job["liveness"], "idle")
        nodes = snap["route"][0]["nodes"]
        self.assertEqual(next(n for n in nodes if n["id"] == "eval-run")["state"], "active")

    def test_long_resource_node_cannot_clip_liveness_or_elapsed_at_card_border(self):
        self.attach(resources=[replace(self.resource, node="very-long-resource-node-" * 8)])
        for width in (60, 80, 168):
            for tint in (False, True):
                with self.subTest(width=width, tint=tint), mock.patch.object(render, "_TINT_OK", tint):
                    rows = render._resource_child_rows(self.owner, term_width=width, in_card=True)
                    framed = [render._frame_dispatch_line(
                        row, render._dispatch_box_width(width), "mid", "frm_idle") for row in rows]
                    self.assertIn("6h 50m", flatten(framed))
                    self.assertLessEqual(sum(render._dw(t) for t, _ in framed[0]), width)

    def test_stage_divider_and_closing_rail_never_repeat_campaign_title(self):
        self.owner.campaign_label = "asr-coresiden-title"
        self.owner.parent_sid = "visible-parent"
        self.owner.is_child = True
        parent = Session(harness="claude", pid=91, cwd=self.owner.cwd,
                         session_id="visible-parent", title="parent", liveness="working")
        for resources in ([self.resource], []):
            self.attach(resources=resources)
            sequence = [("eval-smoke", "done"), ("eval-run", "active"), ("report", "pending")]
            with mock.patch.object(render, "_projection_route_seq", return_value=sequence):
                lines = render._build_lines([parent], [self.owner], "both", False, 0, term_width=168,
                                            resources=resources, governor=None)
            rails = [flatten([line]) for line in lines if line and
                     any(glyph in flatten([line]) for glyph in ("├", "╰"))]
            self.assertTrue(any("eval-smoke" in line for line in rails), flatten(lines))
            self.assertFalse(any("asr-coresiden-title" in line for line in rails))
        self.assertEqual(self.owner.campaign_label, "asr-coresiden-title")

    def test_gpu_resource_metadata_and_progress_share_one_line_and_cpu_stays_separate(self):
        self.resource.elapsed_min = 203
        self.resource.process_group = 41
        self.resource.progress = {"completed": 12, "total": 50, "unit": "epoch", "age_s": 120}
        cpu = replace(self.resource, run_id="cpu-run", node="eval-smoke", pid=77,
                      starttime="777", process_group=77)
        self.attach(resources=[self.resource, cpu])
        process = {"pid": 41, "proc_start": 123, "pgid": 41, "used_memory_mib": 6144,
                   "command": "python train.py", "cwd": "/unrelated"}
        snapshot = {"configured": True, "hosts": [{"host": "mgpu-a1", "self": True,
            "reachable": True, "gpus": [{"index": index, "name": "NVIDIA RTX 6000 Ada",
                                         "processes": [dict(process)]} for index in (0, 1)]}]}
        before = copy.deepcopy(snapshot)
        render.set_compute_hosts(snapshot)
        self.addCleanup(render.set_compute_hosts, None)
        for process_view in (False, True):
            render.set_process_view(process_view)
            for width in (60, 80, 168):
                lines = render._build_lines([], [self.owner], "both", width < 80, 0,
                    layout=render._layout_mode(width), term_width=width,
                    resources=[self.resource, cpu], governor=None)
                gpu = [line for line in lines if line and "● GPU" in flatten([line])]
                self.assertEqual(len(gpu), 1)
                text = flatten(gpu)
                self.assertEqual(text.count("eval run 3h 23m"), 1)
                self.assertNotIn("working", text)
                if width >= 80:
                    self.assertIn("2m ago", text)
                if width >= 168:
                    self.assertIn("12/50 epoch", text)
                    self.assertIn("GPU mgpu-a1:0", text)
                    self.assertIn("GPU mgpu-a1:1", text)
                self.assertNotIn("resource eval run", flatten(lines))
                self.assertEqual(flatten(lines).count("resource eval smoke"), 1)
                self.assertLessEqual(sum(render._dw(t) for t, _ in gpu[0]), width)
        self.assertEqual(snapshot, before)
        self.resource.liveness = "stale"
        rows, cpu_rows = render._owner_gpu_resource_rows(self.owner, {}, {}, 168)
        self.assertIn("eval run stale 3h 23m", flatten(rows))
        self.assertNotIn("resource eval run", flatten(cpu_rows))

    def test_remote_registered_run_matches_gpu_but_cwd_and_reused_pid_do_not(self):
        self.attach()
        snapshot = {"configured": True, "hosts": [{"host": "cnn", "self": False,
            "reachable": True, "gpus": [{"index": 0, "name": "NVIDIA A100", "processes": [{
                "pid": 900, "proc_start": 999, "pgid": 900, "cwd": self.owner.cwd,
                "owner": {"kind": "run", "id": self.resource.run_id}, "used_memory_mib": 12000,
            }]}]}]}
        render.set_compute_hosts(snapshot)
        self.addCleanup(render.set_compute_hosts, None)
        lines = render._build_lines([], [self.owner], "both", False, 0, term_width=168,
                                    resources=[self.resource], governor=None)
        self.assertEqual(sum("● GPU" in flatten([line]) for line in lines if line), 1)
        self.assertIn("GPU cnn:0", flatten(lines))
        snapshot["hosts"][0]["gpus"][0]["processes"][0]["owner"] = None
        self.assertEqual(render._resource_gpu_resources(self.resource, snapshot), [])
        snapshot["hosts"][0]["self"] = True
        item = snapshot["hosts"][0]["gpus"][0]["processes"][0]
        item.update(pid=self.resource.pid, proc_start=456, pgid=900)
        self.assertEqual(render._resource_gpu_resources(self.resource, snapshot), [])

    def test_remote_parent_attempt_merges_resource_and_project_gpu_rows(self):
        self.resource.elapsed_min = 720
        snapshot = {"configured": True, "hosts": [{"host": "xgpu-b", "self": False,
            "reachable": True, "gpus": [{"index": index, "name": "NVIDIA A100", "processes": [{
                "pid": 900, "proc_start": 999, "pgid": 900, "cwd": "/other/project",
                "owner": {"kind": "job", "id": self.owner.attempt_id}, "used_memory_mib": 12000,
            }]} for index in (0, 1)]}]}
        before = copy.deepcopy(snapshot)
        render.set_compute_hosts(snapshot)
        self.addCleanup(render.set_compute_hosts, None)
        for harness in ("claude", "codex", "opencode"):
            self.owner.harness = harness
            self.attach()
            for process_view in (False, True):
                render.set_process_view(process_view)
                for width in (60, 80, 168):
                    with self.subTest(harness=harness, process=process_view, width=width):
                        lines = render._build_lines([], [self.owner], "both", width < 80, 0,
                            layout=render._layout_mode(width), term_width=width,
                            resources=[self.resource], governor=None)
                        gpu = [line for line in lines if line and "● GPU" in flatten([line])]
                        self.assertEqual(len(gpu), 1)
                        self.assertIn("GPU xgpu-b:0", flatten(gpu))
                        self.assertIn("eval run 12h", flatten(gpu))
                        self.assertNotIn("resource eval run", flatten(lines))
                        self.assertNotIn("job:", flatten(gpu))
                        self.assertNotIn("미등록", flatten(lines))
                        self.assertLessEqual(sum(render._dw(t) for t, _ in gpu[0]), width)
        self.assertEqual(snapshot, before)
        for gpu in snapshot["hosts"][0]["gpus"]:
            gpu["processes"] = []
        lines = render._build_lines([], [self.owner], "both", False, 0,
            term_width=168, resources=[self.resource], governor=None)
        self.assertIn("resource eval run", flatten(lines))

    def test_remote_parent_match_requires_remote_host_and_exact_nonempty_attempt(self):
        process = {"pid": 900, "proc_start": 999, "pgid": 900,
                   "owner": {"kind": "job", "id": self.owner.attempt_id}}
        host = {"host": "xgpu-b", "self": False, "reachable": True,
                "gpus": [{"index": 0, "processes": [process]}]}
        snapshot = {"hosts": [host]}
        self.assertTrue(render._resource_gpu_resources(self.resource, snapshot))
        for changes in ({"self": True}, {"self": None}, {"reachable": False}):
            with self.subTest(host=changes):
                self.assertEqual(render._resource_gpu_resources(self.resource, {"hosts": [{**host, **changes}]}), [])
        for attempt in (None, "", "att-other"):
            with self.subTest(attempt=attempt):
                child = replace(self.resource, parent_attempt_id=attempt)
                self.assertEqual(render._resource_gpu_resources(child, snapshot), [])

    def test_drawn_owner_gpu_is_removed_from_parent_but_other_processes_remain(self):
        self.owner.is_child = True
        self.owner.parent_sid = "parent-session"
        for harness in ("claude", "codex", "opencode"):
            self.owner.parent_harness = harness
            parent = Session(harness=harness, pid=91, proc_start="91", cwd=self.owner.cwd,
                             session_id=self.owner.parent_sid, title="parent", liveness="working")
            claim = {"kind": "session", "harness": harness, "id": parent.session_id,
                     "source": "persistent-claim+ancestry"}
            gpu = {"index": 0, "name": "NVIDIA A100", "processes": [{
                "pid": self.resource.pid, "proc_start": 123, "pgid": self.resource.pid,
                "owner": claim, "session_owner": claim, "used_memory_mib": 6144,
            }]}
            snapshot = {"configured": True, "hosts": [{"host": "mgpu-a1", "self": True,
                "reachable": True, "gpus": [gpu]}]}
            render.set_compute_hosts(snapshot)
            self.addCleanup(render.set_compute_hosts, None)
            self.attach()
            for process_view in (False, True):
                render.set_process_view(process_view)
                with self.subTest(harness=harness, process=process_view):
                    lines = render._build_lines([parent], [self.owner], "both", False, 0,
                        term_width=168, resources=[self.resource], governor=None)
                    rows = [line for line in lines if line and "● GPU" in flatten([line])]
                    self.assertEqual(len(rows), 1)
                    self.assertIn("eval run", flatten(rows))
            extra = {"pid": 90, "proc_start": 456, "pgid": 90, "owner": claim,
                     "session_owner": claim, "used_memory_mib": 1024, "command": "python other.py"}
            gpu["processes"].append(extra)
            before = copy.deepcopy(snapshot)
            for process_view in (False, True):
                render.set_process_view(process_view)
                lines = render._build_lines([parent], [self.owner], "both", False, 0,
                    term_width=168, resources=[self.resource], governor=None)
                rows = [line for line in lines if line and "● GPU" in flatten([line])]
                self.assertEqual(len(rows), 2)
                parent_rows = [line for line in rows if "eval run" not in flatten([line])]
                self.assertIn("1 GB", flatten(parent_rows))
                self.assertNotIn("7 GB", flatten(parent_rows))
            self.assertEqual(snapshot, before)
            render.set_process_view(False)
            lines = render._build_lines([parent], [self.owner], "fleet", False, 0,
                term_width=168, resources=[self.resource], governor=None)
            self.assertIn("7 GB", flatten(lines))

    def test_owner_and_remote_resource_share_gpu_without_lost_or_double_counted_memory(self):
        self.owner.is_child = True
        self.owner.parent_sid = "parent-session"
        self.owner._runtime_session_id = "owner-runtime"
        parent = Session(harness="codex", pid=91, proc_start="91", cwd=self.owner.cwd,
                         session_id=self.owner.parent_sid, title="parent", liveness="working")
        job_owner = {"kind": "job", "id": self.owner.attempt_id}
        parent_claim = {"kind": "session", "harness": parent.harness, "id": parent.session_id,
                        "source": "persistent-claim+ancestry"}
        gpu = {"index": 0, "name": "NVIDIA A100", "processes": [
            {"pid": 900, "proc_start": 999, "owner": job_owner, "used_memory_mib": 6144,
             "session_owner": {"kind": "session", "harness": self.owner.harness, "id": "owner-runtime"}},
            {"pid": 901, "proc_start": 1000, "owner": job_owner, "used_memory_mib": 4096,
             "session_owner": parent_claim},
            {"pid": 902, "proc_start": 1001, "owner": parent_claim, "used_memory_mib": 1024,
             "session_owner": parent_claim, "command": "python other.py"},
        ]}
        snapshot = {"configured": True, "hosts": [{"host": "xgpu-b", "self": False,
            "reachable": True, "gpus": [gpu]}]}
        render.set_compute_hosts(snapshot)
        self.addCleanup(render.set_compute_hosts, None)
        second = replace(self.resource, run_id="second-run")
        self.attach(resources=[self.resource, second])
        index = render._gpu_session_resources(snapshot)
        original_index, original_snapshot = copy.deepcopy(index), copy.deepcopy(snapshot)
        with mock.patch.object(render, "_gpu_resource_strip", wraps=render._gpu_resource_strip) as strip:
            render._owner_gpu_resource_rows(self.owner, {}, index, 168)
        combined, = strip.call_args.args[0]
        self.assertEqual((combined["process_count"], combined["used_memory_mib"]), (2, 10240))
        self.assertEqual(index, original_index)
        for process_view in (False, True):
            render.set_process_view(process_view)
            lines = render._build_lines([parent], [self.owner], "both", False, 0,
                term_width=168, resources=[self.resource, second], governor=None)
            rows = [line for line in lines if line and "● GPU" in flatten([line])]
            self.assertEqual(len(rows), 2)
            owner_rows = [line for line in rows if "eval run" in flatten([line])]
            parent_rows = [line for line in rows if "eval run" not in flatten([line])]
            self.assertIn("10 GB", flatten(owner_rows))
            self.assertIn("1 GB", flatten(parent_rows))
        self.assertEqual(snapshot, original_snapshot)


    def test_declared_progress_reaches_now_and_resource_row_in_every_view(self):
        self.attach()
        progress = {"completed": 12, "total": 50, "unit": "epoch", "age_s": 120}
        for process in (False, True):
            render.set_process_view(process)
            for layout, width in (("wide", 168), ("narrow", 80), ("stack", 60)):
                with self.subTest(process=process, width=width):
                    self.resource.progress = None
                    baseline = render._build_lines([], [self.owner], "both", width < 80, 0,
                        layout=layout, term_width=width, resources=[self.resource], governor=None)
                    self.resource.progress = progress
                    actual = render._build_lines([], [self.owner], "both", width < 80, 0,
                        layout=layout, term_width=width, resources=[self.resource], governor=None)
                    self.assertEqual(len(actual), len(baseline))
                    line = next(line for line in actual if line and "resource eval run" in flatten([line]))
                    self.assertIn("2m ago", flatten([line]))
                    self.assertIn("6h 50m", flatten([line]))
                    if width >= 80:
                        self.assertIn("12/50 epoch", flatten([line]))
                    self.assertLessEqual(sum(render._dw(t) for t, _ in line), width)
                    self.assertEqual(flatten(actual).count("2m ago"), 2 if width == 168 else 1)

    def test_counter_without_total_and_old_age_do_not_change_resource_state(self):
        self.attach()
        self.resource.progress = {"completed": 12, "unit": "item", "age_s": 9900}
        text = flatten(render._resource_child_rows(self.owner, term_width=168))
        self.assertIn("12 item · 2h ago", text)
        self.assertEqual(self.resource.liveness, "working")
        self.assertEqual(self.owner.resource_wait["state"], "resource-parked")
        self.resource.progress = None
        self.assertNotIn("ago", flatten(render._resource_child_rows(self.owner, term_width=168)))

    def test_long_counter_unit_keeps_age_and_row_width(self):
        self.attach(resources=[replace(self.resource, node="very-long-resource-node-" * 8)])
        self.owner.resource_children[0].progress = {"completed": 1234567890,
            "total": 1234567890, "unit": "한글" * 20, "age_s": 120}
        for width in (60, 80, 168):
            with self.subTest(width=width):
                rows = render._resource_child_rows(self.owner, term_width=width, in_card=True)
                self.assertEqual(len(rows), 1)
                if width >= 80:
                    self.assertIn("2m ago", flatten(rows))
                self.assertIn("6h 50m", flatten(rows))
                self.assertLessEqual(sum(render._dw(t) for t, _ in rows[0]),
                                     render._dispatch_box_width(width) - 1)


if __name__ == "__main__":
    unittest.main()
