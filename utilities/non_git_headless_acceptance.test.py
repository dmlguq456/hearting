import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "non_git_headless_acceptance", ROOT / "tools/non-git-headless-acceptance.py")
M = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(M)


class FixtureAcceptanceClassificationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.artifact_root = Path(self.tmp.name) / "artifacts"
        decision_rel = "campaigns/c/frame-cycle/artifacts/shards/frame/route-decision.json"
        (self.artifact_root / decision_rel).parent.mkdir(parents=True)
        self.route_id = "rt-" + "a" * 16
        self.leg_id = "rt-" + "b" * 16
        self.digest = "sha256:" + "c" * 64
        (self.artifact_root / decision_rel).write_text(
            json.dumps({"digest": self.digest}), encoding="utf-8")
        routes = self.artifact_root / ".runtime/routes"
        routes.mkdir(parents=True)
        (routes / f"{self.leg_id}.json").write_text(json.dumps({
            "route_id": self.leg_id, "route_hash": "sha256:leg",
            "route_plan": {"decision": decision_rel, "digest": self.digest, "index": 0},
        }), encoding="utf-8")
        link = self.artifact_root / ".runtime/framed-decision" / f"{self.route_id}.leg0.k.route.json"
        link.parent.mkdir(parents=True)
        link.write_text(json.dumps({"route_id": self.leg_id, "route_hash": "sha256:leg"}),
                        encoding="utf-8")
        for rid in (self.route_id, self.leg_id):
            (routes / f"{rid}.outcome.json").write_text(json.dumps({
                "route_id": rid, "terminal_gate_proven": True}), encoding="utf-8")
        self.route = {
            "route_id": self.route_id, "artifact_root": str(self.artifact_root),
            "nodes": [
                {"id": "frame-a", "unit": "plan/frame", "worker_type": "frame"},
                {"id": "route-decision", "kind": "runtime-terminal"},
            ],
        }

    def tearDown(self):
        self.tmp.cleanup()

    def rows(self):
        return [
            {"route_id": self.route_id, "route_node": "frame-a", "worker_type": "frame",
             "status": "done", "failure_class": "pass"},
            {"route_id": self.leg_id, "owner_route_id": self.leg_id, "route_node": "owner",
             "worker_type": "owner", "status": "done", "failure_class": "pass"},
            {"route_id": self.leg_id, "owner_route_id": self.leg_id,
             "route_node": "execute", "worker_type": "stage",
             "status": "done", "failure_class": "pass"},
        ]

    def test_foreign_route_rows_are_ignored(self):
        rows = self.rows()
        rows.append({"route_id": "rt-foreign", "route_node": "frame-b", "worker_type": "frame",
                     "status": "done", "failure_class": "pass"})
        classified = M._classify_fixture_rows(rows, self.route)
        self.assertTrue(classified["verified"])
        self.assertEqual({row["route_id"] for row in classified["frame"]}, {self.route_id})

    def test_missing_expected_frame_node_cannot_verify(self):
        rows = [row for row in self.rows() if row.get("route_node") != "frame-a"]
        classified = M._classify_fixture_rows(rows, self.route)
        self.assertFalse(classified["expected_frames_present"])
        self.assertFalse(classified["verified"])

    def test_leg_owner_and_stage_are_bound_to_the_started_route_plan(self):
        rows = self.rows()
        rows.extend([
            {"route_id": "rt-" + "d" * 16, "owner_route_id": self.route_id,
             "route_node": "owner", "worker_type": "owner", "status": "done",
             "failure_class": "pass"},
            {"route_id": "rt-" + "e" * 16, "owner_route_id": "rt-unrelated",
             "route_node": "execute", "worker_type": "stage", "status": "done",
             "failure_class": "pass"},
        ])
        classified = M._classify_fixture_rows(rows, self.route)
        self.assertTrue(classified["verified"])
        self.assertEqual(classified["started_leg_route_ids"], [self.leg_id])
        self.assertEqual(len(classified["owners"]), 1)
        self.assertEqual(len(classified["stages"]), 1)

    def test_unclosed_leg_route_cannot_verify(self):
        (self.artifact_root / ".runtime/routes" / f"{self.leg_id}.outcome.json").unlink()
        classified = M._classify_fixture_rows(self.rows(), self.route)
        self.assertEqual(classified["leg_routes_closed"], {self.leg_id: False})
        self.assertFalse(classified["verified"])

    def test_leg_with_only_support_workers_cannot_verify(self):
        rows = [dict(row, worker_type="support") if row.get("worker_type") == "stage" else row
                for row in self.rows()]
        self.assertFalse(M._classify_fixture_rows(rows, self.route)["verified"])

    def test_fixture_answer_provenance_is_explicit_and_nonhuman(self):
        provenance = M._frame_answer_provenance(self.route_id)
        self.assertEqual(provenance, {
            "source": "fixture-driver",
            "fixture_gate_actor_kind": "supervisor",
            "person_or_real_supervisor": False,
            "scope": f"isolated fixture route {self.route_id}",
        })

    def test_invalid_missing_proposal_is_not_misclassified_as_person_decision(self):
        result = M._fixture_supervisor_answer(
            {"route_id": self.route_id},
            {"interview_template": {"questions": []},
             "route_proposal_review": {"proposals": [
                 {"node": "frame", "proposal": None, "reason": "block-missing"}]}},
            Path(self.tmp.name) / "route.json", Path(self.tmp.name))
        self.assertEqual(result, "invalid(block-missing)")

    def test_fixture_governor_uses_canonical_artifact_runtime_path(self):
        output = Path(self.tmp.name) / "acceptance-output"
        world, env = M.prepare_fixture(output)
        self.assertEqual(Path(env["AGENT_MODEL_GOVERNOR_ROOT"]),
                         Path(env["AGENT_ARTIFACT_ROOT"]) / ".runtime/model-worker-governor")
        self.assertTrue(Path(env["AGENT_MODEL_GOVERNOR_ROOT"]).is_dir())

    def test_terminal_fixture_owner_is_joined_once_before_rows_are_reloaded(self):
        attempt_id = "att-fixture-owner"
        jobs = Path(self.tmp.name) / "fixture-jobs.log"
        owner = {"attempt_id": attempt_id, "worker_type": "owner", "status": "done"}
        refreshed = [{**owner, "joined": "yes"}]
        env = {"AGENT_DISPATCH_JOBS": str(jobs)}
        events = []

        def run_recorded(output, name, command, **kwargs):
            events.append("join")
            self.assertEqual(name, f"dispatch-wait-{attempt_id}")
            self.assertEqual(command[-8:], [
                "--attempt-id", attempt_id, "--jobs", str(jobs),
                "--interval", "5", "--max", "120"])
            self.assertIs(kwargs["env"], env)
            self.assertEqual(kwargs["timeout"], 650)
            return {"exit_code": 0}

        def route_rows(path):
            events.append("reload")
            self.assertEqual(events, ["join", "reload"])
            self.assertEqual(path, jobs)
            return refreshed

        joined = set()
        with mock.patch.object(M, "_run_recorded", side_effect=run_recorded), \
             mock.patch.object(M, "_route_rows", side_effect=route_rows):
            result = M._join_terminal_fixture_owners(
                [owner], joined=joined, output=Path(self.tmp.name), jobs=jobs,
                fixture_env=env, child_env=env)
            again = M._join_terminal_fixture_owners(
                [owner], joined=joined, output=Path(self.tmp.name), jobs=jobs,
                fixture_env=env, child_env=env)
        self.assertEqual(result, refreshed)
        self.assertEqual(again, [owner])
        self.assertEqual(joined, {attempt_id})
        self.assertEqual(events, ["join", "reload"])

    def test_d3_uses_supported_staged_standard_compose_for_each_mutating_capability(self):
        prompt = Path(self.tmp.name) / "task.md"
        env = {"AGENT_ARTIFACT_ROOT": str(self.artifact_root),
               "AGENT_DISPATCH_JOBS": str(Path(self.tmp.name) / "jobs.log")}
        evidence = Path(self.tmp.name) / "evidence.json"
        cases = (("autopilot-code", "dev"), ("autopilot-apply", ""), ("autopilot-refine", ""))
        for capability, mode in cases:
            with self.subTest(capability=capability):
                command = M._d3_compose_command(capability, mode, prompt, Path(self.tmp.name),
                                                env, evidence, "claude")
                self.assertEqual(command[command.index("--shape") + 1], "staged")
                self.assertEqual(command[command.index("--intensity") + 1], "standard")
                self.assertEqual(command[command.index("--capability") + 1], capability)
                self.assertEqual("--capability-mode" in command, bool(mode))
                if mode:
                    self.assertEqual(command[command.index("--capability-mode") + 1], mode)

    def test_fixture_root_accepts_non_git_parent_and_rejects_checkout(self):
        base = Path(self.tmp.name)
        self.assertEqual(M._resolve_fixture_base(str(base)), base.resolve())
        with self.assertRaisesRegex(ValueError, "outside any Git checkout"):
            M._resolve_fixture_base(str(ROOT))


if __name__ == "__main__":
    unittest.main()
