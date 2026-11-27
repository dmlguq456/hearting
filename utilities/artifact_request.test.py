#!/usr/bin/env python3
"""Request grouping through real frame/compose/producer code, with no model or live I/O.

The shared framed fixture isolates HOME/state/jobs and mocks registered launches.
"""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import importlib.util
import json
import os
from pathlib import Path
import sys
import unittest
from unittest import mock
from concurrent.futures import ThreadPoolExecutor

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
spec = importlib.util.spec_from_file_location("request_framed_fixture", HERE / "framed_start.test.py")
S = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = S
spec.loader.exec_module(S)
P, R, RP = S.P, S.R, S.RP


class RequestFolderTest(S.SourceRepoStartBase):
    def selected(self, legs):
        legs = json.loads(json.dumps(legs))  # no YAML aliases in the synthetic brief
        self.set_briefs(legs, legs)
        self.set_interview({"legs": legs})
        receipt = self.settle()
        self.assertIn(receipt["state"], {"running", "inline"}, receipt)
        path = self.leg_routes()[0]
        leg = json.loads(path.read_text())
        if leg["effective_intensity"] != "direct":
            # The launch is mocked, so perform the owner's real begin here.
            P.begin(self.root, route_file=path, capability=leg["capability"],
                    intensity=leg["effective_intensity"], require_cycle=True)
        return leg, path, receipt

    def cycle(self, route):
        return next(row for row in P.list_cycle_records(self.root) if row["route_id"] == route["route_id"])

    def test_frame_and_execution_share_one_request_folder_without_replacing_frame_manifest(self):
        leg, path, receipt = self.selected([S.CODE_STAGED, S.CODE_STAGED])
        frame = self.cycle(self.route)
        frame_dir = P.cycle_dir(self.root, frame["campaign_id"], frame["cycle_id"])
        before = (frame_dir / "manifest.json").read_bytes()
        execution = self.cycle(leg)
        execution_dir = P.cycle_dir(self.root, execution["campaign_id"], execution["cycle_id"])
        self.assertTrue(execution_dir.is_relative_to(frame_dir), (frame_dir, execution_dir))
        self.assertEqual(execution_dir.relative_to(frame_dir).parts,
                         ("_internal", "executions", execution["cycle_id"]))
        self.assertNotEqual(execution["cycle_id"], frame["cycle_id"])
        self.assertNotEqual(execution["route_hash"], frame["route_hash"])
        self.assertEqual((frame_dir / "manifest.json").read_bytes(), before)
        self.assertEqual(len([p for p in frame_dir.parent.iterdir()
                              if p.is_dir() and (p / ".cycle.json").exists()]), 1)
        resumed = P.begin(self.root, route_file=path, capability=leg["capability"],
                          intensity=leg["effective_intensity"], require_cycle=True)
        self.assertEqual(resumed["cycle_id"], execution["cycle_id"])
        self.assertEqual(Path(resumed["env"]["AGENT_ARTIFACT_OUTPUT_DIR"]), execution_dir / "artifacts")

    def next_execution(self, index, previous, *, slug="another-execution", start=True):
        binding = RP.read_route_plan(f"{self.record_path()}#{index}", self.root)
        previous_dir = P.cycle_dir(self.root, previous["campaign_id"], previous["cycle_id"])
        old_route = json.loads(Path(previous["route_file"]).read_text())
        leg = binding["leg"]
        route = R.compose_route(capability=leg["capability"], capability_mode=leg.get("mode"), shape=leg["shape"],
            graph=",".join(leg["graph"]) if leg.get("graph") else None,
            intensity=leg.get("intensity"), slug=slug, cwd=Path(old_route["cwd"]),
            artifact_root=self.root, spec_read="fixture", campaign_key="framed-key",
            parent_cycle_id=previous["cycle_id"], route_plan=binding,
            **R._leg_evidence(leg, lambda: self.readiness(old_route, self.jobs)))
        path = Path(S.L.admit_runtime_route(self.root, route).route_file)
        if not start:
            return route, path
        result = P.begin(self.root, route_file=path, capability=route["capability"],
                         intensity=route["effective_intensity"], require_cycle=True)
        return route, path, result

    def begin_route(self, route, path):
        return P.begin(self.root, route_file=path, capability=route["capability"],
                       intensity=route["effective_intensity"], require_cycle=True)

    def test_two_executions_keep_separate_manifests_inputs_permissions_and_history(self):
        import artifact_locator
        import artifact_reader
        leg, path, _ = self.selected([S.CODE_STAGED, S.CODE_STAGED])
        first = self.cycle(leg)
        frame = self.cycle(self.route)
        frame_dir = P.cycle_dir(self.root, frame["campaign_id"], frame["cycle_id"])
        frame_manifest = (frame_dir / "manifest.json").read_bytes()
        first_dir = P.cycle_dir(self.root, first["campaign_id"], first["cycle_id"])
        first_output = self.write_output({"cycle_dir": str(first_dir)}, "plans/final_report.md", b"first\n")
        second_route, second_path, second = self.next_execution(1, first)
        second_dir = Path(second["cycle_dir"])
        second_output = self.write_output(second, "plans/final_report.md", b"second\n")
        self.assertNotEqual(first_dir, second_dir)
        for route, record, own, foreign in ((leg, first, first_output, second_output),
                (second_route, second, second_output, first_output),
                (self.route, frame, frame_dir / "artifacts/plans/probe.md", second_output)):
            with mock.patch.dict(os.environ, {"AGENT_ROUTE_ID": route["route_id"],
                    "AGENT_ARTIFACT_CYCLE_ID": record["cycle_id"]}):
                if record is not frame:
                    own_verdict = P.check_write(self.root, own)
                    self.assertEqual(own_verdict["verdict"], "allow", own_verdict)
                    self.assertEqual(own_verdict["cycle_id"], record["cycle_id"])
                self.assertEqual(P.check_write(self.root, foreign)["verdict"], "deny")
        for route, route_path, result in ((leg, path, first), (second_route, second_path, second)):
            self.close(route, route_path)
            P.finalize(self.root, cycle_id=result["cycle_id"], state="completed")
            directory = P.cycle_dir(self.root, result["campaign_id"], result["cycle_id"])
            document = json.loads((directory / "manifest.json").read_bytes())
            self.assertEqual(document["routes"][0]["route_id"], route["route_id"])
            self.assertEqual(document["cycle"]["cycle_id"], result["cycle_id"])
            self.assertTrue(list(S.L.manifest_snapshot_dir(self.root, result["cycle_id"]).glob("*.json")))
            before = (directory / "manifest.json").read_bytes()
            replay = P.finalize(self.root, cycle_id=result["cycle_id"], state="completed")
            self.assertEqual(replay["status"], "already-sealed")
            self.assertEqual((directory / "manifest.json").read_bytes(), before)
        self.assertEqual((frame_dir / "manifest.json").read_bytes(), frame_manifest)
        P.reconcile_root(self.root)
        mapping, _ = artifact_locator.scan_index(self.root)
        for record in (frame, first, second):
            self.assertIn(record["cycle_id"], mapping)
            self.assertEqual(artifact_locator.resolve_path(self.root, record["cycle_id"]),
                P.cycle_dir(self.root, record["campaign_id"], record["cycle_id"]))
        dirs = artifact_reader.cycle_bucket_dirs(self.root, "plans")
        self.assertTrue({first["cycle_id"], second["cycle_id"]}.issubset({r[1]["cycle_id"] for r in dirs}))

    def test_same_name_and_plain_parent_do_not_grant_request_membership(self):
        leg, path, _ = self.selected([S.CODE_STAGED])
        frame = self.cycle(self.route)
        routes = [S.T.compile_for("direct", self.root, slug=self.route["slug"],
            campaign_key="framed-key", parent_cycle_id=frame["cycle_id"], gate_source=source)
            for source in ("separate-request-a", "separate-request-b")]
        for route in routes:
            path = Path(S.L.admit_runtime_route(self.root, route).route_file)
            result = self.begin_route(route, path)
            directory = Path(result["cycle_dir"])
            self.assertEqual(directory.parent, P.cycle_dir(self.root, frame["campaign_id"], frame["cycle_id"]).parent)
            self.assertNotIn("request_binding", self.cycle(route))
        self.assertEqual({self.cycle(r)["locator_suffix"] for r in routes}, {"-2", "-3"})

    def test_concurrent_starts_have_disjoint_scopes_and_exact_retries_reuse_them(self):
        leg, _, _ = self.selected([S.CODE_STAGED, S.CODE_STAGED])
        first = self.cycle(leg)
        pending = [self.next_execution(1, first, slug=slug, start=False)
                   for slug in ("parallel-a", "parallel-b")]
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda args: self.begin_route(*args), pending))
        self.assertEqual(len({r["cycle_id"] for r in results}), 2)
        frame = self.cycle(self.route)
        frame_dir = P.cycle_dir(self.root, frame["campaign_id"], frame["cycle_id"])
        for args, result in zip(pending, results):
            self.assertTrue(Path(result["cycle_dir"]).is_relative_to(frame_dir))
            replay = self.begin_route(*args)
            self.assertEqual((result["cycle_id"], result["cycle_dir"]), (replay["cycle_id"], replay["cycle_dir"]))

    def test_interrupted_begin_repairs_same_logical_cycle_without_losing_other_evidence(self):
        leg, _, _ = self.selected([S.CODE_STAGED, S.CODE_STAGED])
        first = self.cycle(leg)
        proof = self.write_output({"cycle_dir": str(P.cycle_dir(self.root, first["campaign_id"], first["cycle_id"]))},
                                  "plans/final_report.md", b"preserve first\n")
        pending = self.next_execution(1, first, start=False)
        with mock.patch.object(P, "_ensure_dir", side_effect=RuntimeError("interrupted-after-record")):
            with self.assertRaisesRegex(RuntimeError, "interrupted-after-record"):
                self.begin_route(*pending)
        record = self.cycle(pending[0])
        result = self.begin_route(*pending)
        self.assertEqual(result["cycle_id"], record["cycle_id"])
        self.assertTrue((Path(result["cycle_dir"]) / "artifacts").is_dir())
        self.assertTrue((Path(result["cycle_dir"]) / ".cycle.json").is_file())
        self.assertEqual(proof.read_bytes(), b"preserve first\n")
        self.assertEqual(P.read_cycle_record(self.root, first["cycle_id"])["state"], "open")

    def test_unverified_decision_keeps_work_independent_and_existing_executions_visible(self):
        import artifact_locator
        leg, _, _ = self.selected([S.CODE_STAGED, S.CODE_STAGED])
        pending = self.next_execution(1, self.cycle(leg), start=False)
        path = self.record_path()
        original = path.read_bytes()
        changed = json.loads(original)
        changed["decision"]["selected"] = "forged choice"
        path.write_text(json.dumps(changed))
        frame = self.cycle(self.route)
        frame_dir = P.cycle_dir(self.root, frame["campaign_id"], frame["cycle_id"])
        before = {p.relative_to(frame_dir): p.read_bytes() for p in frame_dir.rglob("*") if p.is_file()}
        result = self.begin_route(*pending)
        admitted = self.cycle(pending[0])
        self.assertNotIn("request_binding", admitted)
        self.assertIn("request_binding_note", admitted)
        self.assertFalse(Path(result["cycle_dir"]).is_relative_to(frame_dir))
        # Previously admitted execution identities stay readable. A changed
        # parent input must not erase their manifests or original completion.
        mapping, _ = artifact_locator.scan_index(self.root)
        self.assertIn(self.cycle(leg)["cycle_id"], mapping)
        after = {p.relative_to(frame_dir): p.read_bytes() for p in frame_dir.rglob("*") if p.is_file()}
        self.assertEqual(before, after)
        path.write_bytes(original)
        resumed = self.begin_route(*pending)
        self.assertEqual(resumed["status"], "resumed")
        self.assertEqual(resumed["cycle_dir"], result["cycle_dir"])

    def test_completed_execution_is_still_attestable_after_parent_input_changes(self):
        leg, path, _ = self.selected([S.CODE_STAGED])
        record = self.cycle(leg)
        directory = P.cycle_dir(self.root, record["campaign_id"], record["cycle_id"])
        self.write_output({"cycle_dir": str(directory)}, "plans/final_report.md", b"complete\n")
        self.close(leg, path)
        P.finalize(self.root, cycle_id=record["cycle_id"])
        own_manifest = (directory / "manifest.json").read_bytes()
        self.record_path().unlink()
        # An unrelated damaged parent document cannot invalidate this own
        # cycle's immutable manifest/terminal evidence either.
        frame = self.cycle(self.route)
        frame_dir = P.cycle_dir(self.root, frame["campaign_id"], frame["cycle_id"])
        (frame_dir / "manifest.json").write_text('{"cycle": []}')
        replay = P.finalize(self.root, cycle_id=record["cycle_id"])
        self.assertEqual(replay["status"], "already-sealed")
        self.assertEqual((directory / "manifest.json").read_bytes(), own_manifest)

    def test_changed_capability_and_intensity_keep_distinct_logical_cycles(self):
        strong = {**S.CODE_STAGED, "intensity": "strong"}
        research = {"capability": "autopilot-research", "shape": "solo", "why": "one independent research pass"}
        leg, _, _ = self.selected([S.CODE_STAGED, strong, research])
        records = [self.cycle(leg)]
        for index in (1, 2):
            route, _, _ = self.next_execution(index, records[-1], slug=f"leg-{index}")
            records.append(self.cycle(route))
        self.assertEqual(len({r["cycle_id"] for r in records}), 3)
        self.assertEqual([r["intensity"] for r in records], ["standard", "strong", "quick"])
        self.assertEqual(records[-1]["capability"], "autopilot-research")
        self.assertEqual(len({r["request_binding"]["cycle_id"] for r in records}), 1)

    def test_request_rename_and_index_rebuild_preserve_completed_member(self):
        import artifact_locator
        leg, path, _ = self.selected([S.CODE_STAGED])
        record = self.cycle(leg)
        frame = self.cycle(self.route)
        frame_dir = P.cycle_dir(self.root, frame["campaign_id"], frame["cycle_id"])
        directory = P.cycle_dir(self.root, record["campaign_id"], record["cycle_id"])
        self.write_output({"cycle_dir": str(directory)}, "plans/final_report.md", b"preserved\n")
        self.close(leg, path)
        P.finalize(self.root, cycle_id=record["cycle_id"])
        before = (directory / "manifest.json").read_bytes()
        moved = frame_dir.with_name("2026-11-06_renamed-request")
        frame_dir.rename(moved)
        (self.root / "campaigns/INDEX.json").unlink()
        mapping, _ = artifact_locator.scan_index(self.root)
        expected = moved / "_internal/executions" / record["cycle_id"]
        self.assertEqual(self.root / mapping[record["cycle_id"]], expected)
        P.reconcile_root(self.root)
        self.assertEqual(P.cycle_dir(self.root, record["campaign_id"], record["cycle_id"]), expected)
        self.assertEqual((expected / "manifest.json").read_bytes(), before)
        self.assertEqual(P.finalize(self.root, cycle_id=record["cycle_id"])["status"], "already-sealed")

    def test_pending_finish_fences_the_execution_instead_of_its_frame(self):
        import inline_finish
        leg, _, _ = self.selected([S.CODE_STAGED, S.CODE_STAGED])
        first = self.cycle(leg)
        next_route, _, second = self.next_execution(1, first)
        for route, record in ((leg, first), (next_route, second)):
            target = Path(P.cycle_dir(self.root, record["campaign_id"], record["cycle_id"])) / "artifacts/plans/new.md"
            with mock.patch.object(inline_finish, "pending_for_cycle", side_effect=lambda root, cid:
                    {"state": "claimed"} if cid == first["cycle_id"] else None), \
                    mock.patch.dict(os.environ, {"AGENT_ROUTE_ID": route["route_id"],
                                                "AGENT_ARTIFACT_CYCLE_ID": record["cycle_id"]}):
                verdict = P.check_write(self.root, target)
            if record is first:
                self.assertEqual(verdict.get("reason"), "finish-in-progress", verdict)
            else:
                self.assertEqual(verdict["verdict"], "allow", verdict)

    def test_frame_cannot_write_execution_controls_even_before_a_member_marker_exists(self):
        leg, _, _ = self.selected([S.CODE_STAGED])
        frame = self.cycle(self.route)
        directory = P.cycle_dir(self.root, frame["campaign_id"], frame["cycle_id"])
        own = directory / "artifacts/plans/own.md"
        self.assertEqual(P.require_cycle_output(self.root, own, cycle_id=frame["cycle_id"]), directory / "artifacts")
        unknown = directory / "_internal/executions" / ("cyc_" + "f" * 32) / "manifest.json"
        with self.assertRaises(P.ProducerError) as caught:
            P.require_cycle_output(self.root, unknown, cycle_id=frame["cycle_id"])
        self.assertEqual(caught.exception.code, "artifact-outside-bound-cycle")

    def test_planned_direct_uses_journal_without_a_second_cycle(self):
        leg, path, receipt = self.selected([S.DIRECT])
        import artifact_journal
        binding = artifact_journal.target(self.root, leg["route_id"])
        self.assertIsNotNone(binding)
        self.assertEqual(len(P.list_cycle_records(self.root)), 1)
        self.assertEqual(binding["request"]["cycle_id"], self.cycle(self.route)["cycle_id"])
        self.assertEqual(binding["parent_cycle_id"], leg["parent_cycle_id"])
        origin = {"kind": "finish", "route_id": leg["route_id"]}
        result = artifact_journal.append(self.root, binding, origin=origin, body="done\n", summary="done")
        self.assertEqual(result["request"], binding["request"])
        repeated = artifact_journal.append(self.root, binding, origin=origin, body="done\n", summary="done")
        self.assertTrue(repeated["replay"])
        self.assertEqual(len(artifact_journal.read_day(self.root / binding["path"])["rows"]), 1)

    def completed_request(self):
        leg, path, _ = self.selected([S.CODE_STAGED])
        record = self.cycle(leg)
        frame = self.cycle(self.route)
        directory = P.cycle_dir(self.root, record["campaign_id"], record["cycle_id"])
        self.write_output({"cycle_dir": str(directory)}, "plans/final_report.md", b"complete\n")
        self.close(leg, path)
        P.finalize(self.root, cycle_id=record["cycle_id"])
        return frame, record

    def target_campaign(self):
        route = S.T.compile_for("direct", self.root, slug="target", campaign_key="target-campaign")
        path = Path(S.L.admit_runtime_route(self.root, route).route_file)
        return self.begin_route(route, path)

    def test_cross_campaign_request_move_updates_every_logical_member(self):
        frame, record = self.completed_request()
        target = self.target_campaign()
        source = P.cycle_dir(self.root, frame["campaign_id"], frame["cycle_id"])
        destination = P.campaign_dir(self.root, target["campaign_id"]) / source.name
        source.rename(destination)
        result = P.reconcile_root(self.root)
        self.assertNotEqual(result["status"], "skipped", result)
        self.assertEqual(P.read_cycle_record(self.root, record["cycle_id"])["campaign_id"], target["campaign_id"])
        self.assertNotIn(record["cycle_id"], P.read_campaign(self.root, frame["campaign_id"])["cycles"])
        self.assertIn(record["cycle_id"], P.read_campaign(self.root, target["campaign_id"])["cycles"])
        self.assertEqual(P.finalize(self.root, cycle_id=record["cycle_id"])["status"], "already-sealed")

    def test_deleting_request_preserves_member_history_and_settles_its_deleted_record(self):
        frame, record = self.completed_request()
        snapshots = {p.name: p.read_bytes() for p in S.L.manifest_snapshot_dir(self.root, record["cycle_id"]).glob("*.json")}
        P.delete_cycle(self.root, frame["cycle_id"], reason="delete this physical request fixture")
        self.assertTrue(P.read_cycle_record(self.root, record["cycle_id"]).get("deleted_at"))
        self.assertNotIn(record["cycle_id"], P.read_campaign(self.root, frame["campaign_id"])["cycles"])
        self.assertEqual(snapshots, {p.name: p.read_bytes() for p in S.L.manifest_snapshot_dir(self.root, record["cycle_id"]).glob("*.json")})
        self.assertEqual(P.finalize(self.root, cycle_id=record["cycle_id"])["status"], "already-sealed")

    def test_explicit_request_move_updates_every_member_and_keeps_original_snapshots(self):
        frame, record = self.completed_request()
        target = self.target_campaign()
        snapshots = {p.name: p.read_bytes() for p in S.L.manifest_snapshot_dir(self.root, record["cycle_id"]).glob("*.json")}
        P.cycle_move(self.root, frame["cycle_id"], campaign=target["campaign_id"], reason="move request fixture")
        self.assertEqual(P.read_cycle_record(self.root, record["cycle_id"])["campaign_id"], target["campaign_id"])
        self.assertIn(record["cycle_id"], P.read_campaign(self.root, target["campaign_id"])["cycles"])
        for name, raw in snapshots.items():
            self.assertEqual((S.L.manifest_snapshot_dir(self.root, record["cycle_id"]) / name).read_bytes(), raw)
        self.assertEqual(P.finalize(self.root, cycle_id=record["cycle_id"])["status"], "already-sealed")

    def order_of_keeping_and_reading(self):
        """Record, in order, each manifest kept and each time the cycle leaves the member list of a campaign."""
        import artifact_producer_cycle_ops as OPS
        order = []
        real_keep, real_edit = OPS._preserve_current_manifest, OPS._edit_campaign_members

        def keep(root, record, directory, **kwargs):
            order.append(("kept", record["cycle_id"]))
            return real_keep(root, record, directory, **kwargs)

        def edit(root, folder, cycle_id, *, joining):
            order.append(("joined" if joining else "left", cycle_id))
            return real_edit(root, folder, cycle_id, joining=joining)
        return order, mock.patch.object(OPS, "_preserve_current_manifest", keep), \
            mock.patch.object(OPS, "_edit_campaign_members", edit)

    def assert_members_kept_before_the_old_campaign_is_read(self, order, frame, record):
        """The old campaign is read (the cycle leaves its list) only after every cycle of the request kept its manifest."""
        first_read = next(i for i, (what, _cycle) in enumerate(order) if what == "left")
        kept_before = {cycle for what, cycle in order[:first_read] if what == "kept"}
        self.assertEqual(kept_before, {frame["cycle_id"], record["cycle_id"]}, order)

    def test_request_move_keeps_every_member_manifest_before_the_old_campaign_is_read(self):
        frame, record = self.completed_request()
        target = self.target_campaign()
        order, keep, edit = self.order_of_keeping_and_reading()
        with keep, edit:
            P.cycle_move(self.root, frame["cycle_id"], campaign=target["campaign_id"])
        self.assert_members_kept_before_the_old_campaign_is_read(order, frame, record)

    def test_hand_moved_request_keeps_every_member_manifest_before_the_old_campaign_is_read(self):
        frame, record = self.completed_request()
        target = self.target_campaign()
        source = P.cycle_dir(self.root, frame["campaign_id"], frame["cycle_id"])
        source.rename(P.campaign_dir(self.root, target["campaign_id"]) / source.name)
        order, keep, edit = self.order_of_keeping_and_reading()
        with keep, edit:
            self.assertEqual(P.reconcile_root(self.root)["status"], "reconciled")
        self.assert_members_kept_before_the_old_campaign_is_read(order, frame, record)

    def test_request_move_does_not_use_whether_a_member_manifest_was_kept(self):
        # As for the cycle itself: keeping the bytes is attempted and its answer is not a reason to stop the
        # move (there is no refusal of its own; a document that cannot be replaced is met where it is replaced).
        import artifact_producer_cycle_ops as OPS
        frame, record = self.completed_request()
        target = self.target_campaign()
        source = P.cycle_dir(self.root, frame["campaign_id"], frame["cycle_id"])
        kept = []

        def cannot_keep(root, rec, directory, **kwargs):
            kept.append(rec["cycle_id"])
            return False
        with mock.patch.object(OPS, "_preserve_current_manifest", side_effect=cannot_keep):
            moved = P.cycle_move(self.root, frame["cycle_id"], campaign=target["campaign_id"])
        self.assertEqual(moved["status"], "moved")
        self.assertIn(record["cycle_id"], kept)  # the member's bytes were still asked to be kept
        self.assertFalse(source.exists())

    def test_moved_open_member_resumes_same_cycle_in_current_campaign(self):
        leg, path, _ = self.selected([S.CODE_STAGED])
        record = self.cycle(leg)
        frame = self.cycle(self.route)
        target = self.target_campaign()
        P.cycle_move(self.root, frame["cycle_id"], campaign=target["campaign_id"])
        try:
            resumed = self.begin_route(leg, path)
        except P.ProducerError as exc:
            resumed = {"status": exc.code}
        self.assertEqual(resumed["status"], "resumed", resumed)
        self.assertEqual(resumed["cycle_id"], record["cycle_id"])
        self.assertEqual(resumed["campaign_id"], target["campaign_id"])

    def interrupted_delete(self, *, after_removal):
        import artifact_admission
        frame, record = self.completed_request()
        real = P._remove_folder
        def fail(root, directory):
            if after_removal:
                real(root, directory)
            raise RuntimeError("interrupted-delete")
        with S.patch_split(P, "_remove_folder", side_effect=fail):
            with self.assertRaisesRegex(RuntimeError, "interrupted-delete"):
                P.delete_cycle(self.root, frame["cycle_id"])
        self.assertTrue(P.read_cycle_record(self.root, record["cycle_id"]).get("deleted_at"))
        P.delete_cycle(self.root, frame["cycle_id"])
        self.assertNotIn(record["cycle_id"], artifact_admission.load_index(self.root).cycles)
        self.assertNotIn(record["cycle_id"], P.read_campaign(self.root, frame["campaign_id"])["cycles"])
        self.assertTrue(P.finalize(self.root, cycle_id=record["cycle_id"])["deleted"])

    def test_request_delete_resumes_before_physical_removal(self):
        self.interrupted_delete(after_removal=False)

    def test_request_delete_resumes_after_physical_removal(self):
        self.interrupted_delete(after_removal=True)

    # A sealed frame's refresh lists the frame's own files, never an execution's (CORE.md "Logical
    # executions in one request folder": each execution keeps its own manifest).
    def frame_paths(self, frame_dir):
        document = json.loads((frame_dir / "manifest.json").read_bytes())
        return {row["locator"]["path"] for row in document["artifact_revisions"]}

    def sealed_frame_with_execution(self, legs=(S.CODE_STAGED,), *, extra_files=0):
        leg, path, _ = self.selected(list(legs))
        frame, execution = self.cycle(self.route), self.cycle(leg)
        frame_dir = P.cycle_dir(self.root, frame["campaign_id"], frame["cycle_id"])
        execution_dir = P.cycle_dir(self.root, execution["campaign_id"], execution["cycle_id"])
        self.write_output({"cycle_dir": str(execution_dir)}, "plans/final_report.md", b"execution\n")
        for index in range(extra_files):
            (execution_dir / "artifacts" / "plans" / f"f{index:04d}.md").write_bytes(b"x\n")
        self.close(leg, path)
        P.finalize(self.root, cycle_id=execution["cycle_id"], state="completed")
        self.assertEqual(P.read_cycle_record(self.root, frame["cycle_id"])["state"], "sealed")
        self.assertEqual({p for p in self.frame_paths(frame_dir) if p.startswith("_internal/executions/")}, set())
        return frame, execution, frame_dir, execution_dir

    def assert_frame_note_in_and_no_execution_rows(self, frame_dir, result):
        self.assertEqual(result["status"], "emitted", result)
        paths = self.frame_paths(frame_dir)
        self.assertIn("_internal/frame-note.md", paths)
        self.assertEqual({p for p in paths if p.startswith("_internal/executions/")}, set(), result)

    def test_a_frame_refresh_takes_in_its_own_late_file_and_none_of_its_executions_files(self):
        frame, execution, frame_dir, execution_dir = self.sealed_frame_with_execution()
        (frame_dir / "_internal" / "frame-note.md").write_bytes(b"the frame's own late file\n")
        result = P.refresh_cycle(self.root, frame["cycle_id"], trigger="explicit")
        self.assert_frame_note_in_and_no_execution_rows(frame_dir, result)
        execution_manifest = json.loads((execution_dir / "manifest.json").read_bytes())
        self.assertEqual(execution_manifest["cycle"]["cycle_id"], execution["cycle_id"])
        self.assertIn("artifacts/plans/final_report.md",
                      {row["locator"]["path"] for row in execution_manifest["artifact_revisions"]})

    def test_an_execution_begun_after_the_member_list_was_read_waits_for_the_next_refresh(self):
        import artifact_producer_refresh as REF
        frame, execution, frame_dir, _ = self.sealed_frame_with_execution([S.CODE_STAGED, S.CODE_STAGED])
        second_route, second_path = self.next_execution(1, execution, start=False)
        (frame_dir / "_internal" / "frame-note.md").write_bytes(b"the frame's own late file\n")
        real, begun = REF._request_member_dirs, []

        def read_then_begin(directory):
            members = real(directory)
            if not begun:  # a normal begin finishes right after the member list was read
                begun.append(self.begin_route(second_route, second_path))
                self.write_output(begun[0], "plans/concurrent-member.md", b"second\n")
            return members
        with mock.patch.object(REF, "_request_member_dirs", side_effect=read_then_begin):
            first = P.refresh_cycle(self.root, frame["cycle_id"])
        self.assertEqual((first["status"], first.get("reason")), ("skipped", "superseded"), first)
        self.assertEqual({p for p in self.frame_paths(frame_dir) if p.startswith("_internal/")}, set())
        self.assert_frame_note_in_and_no_execution_rows(frame_dir, P.refresh_cycle(self.root, frame["cycle_id"]))

    def test_support_files_of_an_ordinary_cycle_in_the_same_shape_are_listed_and_refreshed_as_before(self):
        import time
        import artifact_producer_refresh as REF
        frame, execution, frame_dir, _ = self.sealed_frame_with_execution()
        route = S.T.compile_for("direct", self.root, slug="ordinary", campaign_key="ordinary-campaign")
        route_path = Path(S.L.admit_runtime_route(self.root, route).route_file)
        ordinary = self.begin_route(route, route_path)
        self.write_output(ordinary, "plans/final_report.md", b"ordinary\n")
        self.close(route, route_path)
        P.finalize(self.root, cycle_id=ordinary["cycle_id"], state="completed")
        self.assertEqual(P.read_cycle_record(self.root, ordinary["cycle_id"])["state"], "sealed")
        support = Path(ordinary["cycle_dir"]) / "_internal" / "executions" / execution["cycle_id"]
        support.mkdir(parents=True)
        (support / "support.md").write_bytes(b"ordinary support\n")
        result = P.refresh_cycle(self.root, ordinary["cycle_id"])
        self.assertEqual(result["status"], "emitted", result)
        self.assertIn(f"_internal/executions/{execution['cycle_id']}/support.md", self.frame_paths(Path(ordinary["cycle_dir"])))
        # Mid copy-move (the record names its new root, the source folder is still here) nothing changed,
        # and the member check is made in the recorded folder, so nothing is looked up.
        record = P.read_cycle_record(self.root, ordinary["cycle_id"])
        P._write_cycle_record(self.root, dict(record, relocation={"artifact_root": "/elsewhere"}), exclusive=False)
        later = time.time() + REF.checkpoint_interval_seconds() + 1
        self.assertEqual(P.refresh_cycle(self.root, ordinary["cycle_id"])["status"], "unchanged")
        self.assertEqual(P.refresh_cycle(self.root, ordinary["cycle_id"], trigger="turn-end", now=later)["status"],
                         "unchanged")
        refinalized = P.finalize(self.root, cycle_id=ordinary["cycle_id"])
        self.assertEqual((refinalized["status"], refinalized["refreshed"]), ("already-sealed", False), refinalized)

    def test_a_frame_moved_in_its_campaign_and_linked_from_its_old_name_refreshes_as_before(self):
        # Left as it was: the shared member check refuses the linked recorded folder, so it finds no member
        # and the refresh lists the execution's files as it did before this change.
        frame, execution, frame_dir, _ = self.sealed_frame_with_execution()
        moved = frame_dir.with_name(frame_dir.name + "-moved")
        frame_dir.rename(moved)
        frame_dir.symlink_to(moved.name, target_is_directory=True)  # the record still names the old one
        (moved / "_internal" / "frame-note.md").write_bytes(b"the frame's own late file\n")
        result = P.refresh_cycle(self.root, frame["cycle_id"])
        self.assertEqual(result["status"], "emitted", result)
        member = f"_internal/executions/{execution['cycle_id']}/"
        self.assertEqual(sorted(result["changes"]["added"]), sorted(
            ["_internal/frame-note.md", member + "artifacts/plans/final_report.md", member + "manifest.json"]))

    def test_a_linked_artifact_root_and_a_relative_frame_path_leave_executions_out(self):
        import contextlib
        import tempfile
        import artifact_producer_store as STORE
        frame, _execution, frame_dir, _ = self.sealed_frame_with_execution()
        relative = frame_dir.relative_to(self.root)
        with contextlib.chdir(self.root):  # both walkers compare the same cycle-relative form
            self.assertEqual(STORE._request_member_dirs(relative), STORE._request_member_dirs(frame_dir))
            walked = {path.relative_to(relative).as_posix() for path in P._walk_cycle_files(relative)}
            self.assertEqual({p for p in walked if p.startswith("_internal/executions/")}, set())
        links = tempfile.TemporaryDirectory()
        self.addCleanup(links.cleanup)
        linked = Path(links.name) / "root"
        linked.symlink_to(self.root, target_is_directory=True)
        (frame_dir / "_internal" / "frame-note.md").write_bytes(b"the frame's own late file\n")
        self.assert_frame_note_in_and_no_execution_rows(frame_dir, P.refresh_cycle(linked, frame["cycle_id"]))

    def test_old_execution_rows_do_not_spend_the_absence_check_and_a_refinalize_defers_as_on_main(self):
        import artifact_producer_refresh as REF
        # More old member rows than the absence check's floor plus the 22 walk entries a turn-end run has left.
        frame, _execution, frame_dir, execution_dir = self.sealed_frame_with_execution(
            extra_files=P.REFRESH_ABSENCE_CHECK_FLOOR + 30)
        frame_id, removed = frame["cycle_id"], frame_dir / "_internal" / "z-removed.txt"
        removed.write_bytes(b"the frame's own file\n")
        with mock.patch.object(REF, "_request_member_dirs", return_value=set()):  # the refresh before this change
            self.assertEqual(P.refresh_cycle(self.root, frame_id)["status"], "emitted")
        member = execution_dir.relative_to(frame_dir).as_posix() + "/"
        old_rows = {p for p in self.frame_paths(frame_dir) if p.startswith(member)}
        self.assertGreater(len(old_rows), P.REFRESH_ABSENCE_CHECK_FLOOR + 22)
        for cursor in (None, member + "artifacts/plans/f0500.md"):  # from the top, and resumed inside the old member
            if cursor is not None:
                removed.write_bytes(b"the frame's own file again\n")
                self.assertEqual(P.refresh_cycle(self.root, frame_id)["changes"]["added"], ["_internal/z-removed.txt"])
            removed.unlink()
            state = REF._read_refresh_state(self.root, frame_id)
            REF._write_refresh_state(self.root, frame_id, cursor=cursor, files=state.get("files", {}), observed_at=None)
            result = P.refresh_cycle(self.root, frame_id, trigger="turn-end", budget=P.RefreshBudget(max_walk_entries=22))
            self.assertEqual(result["status"], "emitted", (cursor, result))
            self.assertEqual(result["changes"]["removed"], ["_internal/z-removed.txt"], cursor)
            self.assertEqual(result["changes"]["reclassified"], [], cursor)
            self.assertEqual({p for p in self.frame_paths(frame_dir) if p.startswith(member)}, old_rows, cursor)
        # A refinalize keeps main's path: the old rows the seal walk leaves out look removed, the files are there,
        # so it defers and the frame's late file waits for a refresh. The old rows stay as they are.
        (frame_dir / "_internal" / "frame-note.md").write_bytes(b"the frame's own late file\n")
        refinalized = P.finalize(self.root, cycle_id=frame_id)
        self.assertEqual((refinalized.get("refreshed"), refinalized.get("deferred")),
                         (False, "files-changed-during-read"), refinalized)
        self.assertNotIn("_internal/frame-note.md", self.frame_paths(frame_dir))
        self.assertEqual({p for p in self.frame_paths(frame_dir) if p.startswith(member)}, old_rows)


if __name__ == "__main__":
    unittest.main()
