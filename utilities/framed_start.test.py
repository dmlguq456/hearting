#!/usr/bin/env python3
"""The framed flow from the frame briefs to the first leg: proposals, the decision record, the
first-leg transaction, every none ending, the failure table and restart at each boundary.

Real compose, route files, producer cycles, completion/close/finalize code and a real record on
disk. Controlled here: the two frame markers (their gate), the registered launch (a fake `run`
that registers one row like the selector does), and the readiness probe (fixed evidence).
"""
import contextlib
import copy
import importlib.util
import io
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

# The card screens these tests compare are Korean and UI text defaults to English,
# so this suite declares the Korean operator language itself (tools/i18n/ui_text.py).
os.environ["FLEET_NOW_LANG"] = "Korean"

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "tools"))
from split_module_fixture import patch_split, split_patch  # noqa: E402


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, HERE / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


F = _load("framed_route_for_framed_start", "framed_route.test.py")
T, R, P, L = F.T, F.R, F.P, F.L
import route_plan as RP  # noqa: E402
import work_start as W  # noqa: E402
import frame_interview as FI  # noqa: E402
import dispatch_contract as D  # noqa: E402
from route_parent_close import owner_continuation_processes as REAL_OWNER_OBSERVATION  # noqa: E402


def owner_observation(meta, jobs):
    """Observe the fake launch row; exact PID fixtures keep the real observer."""
    if meta.get("pid"):
        return REAL_OWNER_OBSERVATION(meta, jobs)
    status = W._rows(jobs).get(meta.get("attempt_id"), ("done", {}))[0]
    return D.ProcessQuiescence("live" if status in {"open", "running"} else "quiescent",
                               "fixture-observation"), []

# `StartBase` replaces `R.proposal_readiness` with a fixture; the real function stays reachable here.
REAL_PROPOSAL_READINESS = R.proposal_readiness
DIRECT = {"capability": "autopilot-code", "shape": "direct", "why": "one small edit"}
CODE_STAGED = {"capability": "autopilot-code", "mode": "dev", "shape": "staged",
               "graph": ["plan", "execute", "test", "report"], "why": "a bug with a test"}
LAB_SETUP = {"capability": "autopilot-lab", "mode": "setup", "shape": "staged",
             "graph": ["scaffold", "smoke", "full-run", "run-verify", "handoff"]}
ROUTE_LABELS = ("예, 이 경로로 진행", "아니요, 다르게 할게요")


def brief(legs, *, summary="Do the work", approvals=None, section=True, tail=""):
    """A direction brief whose section 8 carries one route_proposal_v1 block."""
    body = {"summary": summary, "legs": legs}
    if approvals:
        body["entry_approvals"] = approvals
    import yaml
    text = yaml.safe_dump({"route_proposal_v1": body}, sort_keys=False, allow_unicode=True)
    head = "---\nstatus: framed\n---\n\n## 1. Problem\n\nx\n\n## 7. Questions\n\nnone\n\n"
    if not section:
        return head + "## 8. Something else\n\nnothing\n"
    return head + "## 8. 경로 조립 제안\n\n```yaml\n" + text + "```\n" + tail


def question(qid, *, kind="yes-no", topic=None, labels=("네", "아니요"), approves=None):
    """`approves` is the index of the option that approves (an approval question carries exactly one)."""
    return {"id": qid, "topic": topic or qid, "question": f"{qid}?", "kind": kind,
            "options": [{"label": label, "means": label, **({"approves": True} if at == approves else {})}
                        for at, label in enumerate(labels)],
            "recommended": 0, "why": "only you can say"}


class StartBase(F.EndingBase):
    """A framed route whose two frames are done, with hooks for briefs, interview and first leg."""

    def setUp(self):
        super().setUp()
        self.leg_calls = []
        self.fault = None
        W._ROUTE_MODULE = R
        self.addCleanup(setattr, W, "_ROUTE_MODULE", None)
        self.addCleanup(setattr, W, "FAULT_HOOK", None)
        patch = mock.patch.object(R, "proposal_readiness", create=False, side_effect=self.readiness)
        patch.start()
        self.addCleanup(patch.stop)
        real_prepare = R.prepare_isolated_worktree

        def prepare_fixture(cwd, slug, **kwargs):
            target, repository = str(Path(cwd).resolve()), str(R.ROOT.resolve())
            if os.path.commonpath([target, repository]) == repository:
                return {"state": "skipped", "reason": "fixture-cwd-is-not-a-git-allocation-target"}
            return real_prepare(cwd, slug, **kwargs)

        patch = mock.patch.object(R, "prepare_isolated_worktree", side_effect=prepare_fixture)
        patch.start()
        self.addCleanup(patch.stop)
        patch = mock.patch.object(W, "_recorded_interview", side_effect=lambda route, jobs: (self.interview, self.answers))
        patch.start()
        self.addCleanup(patch.stop)
        patch = mock.patch.object(W, "default_parent_session_id", return_value="parent")
        patch.start()
        self.addCleanup(patch.stop)
        self.interview = self.answers = None
        patch = mock.patch("route_parent_close.owner_continuation_processes", side_effect=owner_observation)
        patch.start()
        self.addCleanup(patch.stop)
        self.set_briefs(DIRECT, DIRECT)
        self.set_interview({"legs": [DIRECT]})

    def readiness(self, frame_route, jobs):
        return {"tuples": [T.nested("claude", "codex")], "candidates": T.registered_headless()["candidates"]}

    # -- fixtures --------------------------------------------------------------------------
    def set_briefs(self, first, second, **kw):
        for node, leg in zip(F.FRAME_IDS, (first, second)):
            text = leg if isinstance(leg, str) else brief(leg if isinstance(leg, list) else [leg], **kw)
            (self.output / "shards" / node / "direction-brief.md").write_text(text, encoding="utf-8")

    def set_interview(self, proposal, *, choice=0, route_choice=None, extra_questions=(), summary="Do the work",
                      approval_choice=None):
        """An interview carrying `route_proposals` for `proposal` and the answers to it."""
        proposal = {"summary": summary, "entry_approvals": [], **proposal}
        questions = [question("route", labels=ROUTE_LABELS), *extra_questions]
        self.interview = {
            "schema": FI.SCHEMA, "route_id": self.route["route_id"], "round": 1, "questions": questions,
            "route_proposals": {"question": "route", "by_option": {ROUTE_LABELS[0]: proposal}}}
        given = {"route": {"choice": choice if route_choice is None else route_choice, "note": ""}}
        for extra in extra_questions:
            given[extra["id"]] = {"choice": 0 if approval_choice is None else approval_choice, "note": ""}
        self.answers = {"schema": FI.ANSWERS_SCHEMA, "route_id": self.route["route_id"], "round": 1,
                        "actor_kind": "user", "understanding_confirmed": True, "correction": "", "answers": given}

    def fake_run(self, command, **kwargs):
        """The selector admits the first leg's owner: one registered row."""
        self.leg_calls.append(command)
        value = lambda flag: command[command.index(flag) + 1]
        route = json.loads(Path(value("--route-evidence")).read_text(encoding="utf-8"))
        meta = {"attempt_id": value("--attempt-id"), "parent_sid": "parent", "launch_started": "1",
                "worker_type": "owner", "dispatch_depth": "1", "route_id": route["route_id"],
                "route_hash": route["route_hash"], "owner_route_id": route["route_id"],
                "owner_route_hash": route["route_hash"],
                "parent_completion_delivery": "codex-managed-gateway"}
        with self.jobs.open("a") as stream:
            stream.write("now\topen\t12\tparent\ttask\t" + ",".join(k + "=" + v for k, v in meta.items()) + "\n")
        return subprocess.CompletedProcess(command, 0, "registered=1 started=1 child_spawned=1\n", "")

    def settle(self, **kw):
        kw.setdefault("run", self.fake_run)
        with mock.patch.object(W, "join_selected_attempts", return_value={"state": "timeout", "children": []}), \
                mock.patch.object(W, "parent_next", return_value=("end-turn", "fixture", "")):
            return super().settle(**kw)

    # -- reads -----------------------------------------------------------------------------
    def record(self):
        return RP.read_record(self.record_path())

    def leg_routes(self):
        return sorted(p for p in (self.root / ".runtime" / "routes").glob("rt-*.json")
                      if re.fullmatch(r"rt-[0-9a-f]{16}\.json", p.name) and p.stem != self.route["route_id"])

    def registry_rows(self):
        return [line for line in self.jobs.read_text().splitlines() if line.strip()]

    def finish_leg(self, route, route_file):
        """What a finished leg leaves behind: its proven-closed route and its sealed producer cycle."""
        if not any(r.get("route_id") == route["route_id"] for r in P.list_cycle_records(self.root)):
            self.begin_cycle(route_file)           # a registered owner opens its own cycle when it starts
        self.close(route, route_file)
        record = next(r for r in P.list_cycle_records(self.root) if r.get("route_id") == route["route_id"])
        output = Path(P.cycle_dir(self.root, record["campaign_id"], record["cycle_id"], record)) / "artifacts/documents/done.md"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text("done\n", encoding="utf-8")
        P.finalize(self.root, cycle_id=record["cycle_id"], state="completed")
        return record["cycle_id"]


class EntryExecutionScopeTest(StartBase):
    def test_report_choice_compiles_first_leg_instead_of_becoming_none(self):
        approvals = [{"key": "full-run", "leg": 0, "question": "route"}]
        self.set_briefs(LAB_SETUP, LAB_SETUP, approvals=approvals)
        self.set_interview({"legs": [LAB_SETUP], "execution_scope": "report", "entry_approvals": approvals})
        result = self.settle()
        self.assertNotEqual(result.get("state"), "none", result)
        record = self.record()
        self.assertEqual(record["decision"]["selected"], ROUTE_LABELS[0])
        self.assertEqual(record["decision"]["approvals"]["execution_scope"], "report")
        self.assertTrue(record.get("first_leg"))
        self.assertEqual(record["decision"]["first_leg_compose"]["graph"], ["scaffold", "smoke"])

    def test_quick_refine_complete_and_report_compile_without_the_new_preview_wait(self):
        for scope in ("complete", "report"):
            with self.subTest(scope=scope):
                self.tearDown()
                self.setUp()
                approvals = [{"key": "preview", "leg": 0, "question": "route"}]
                refine = {"capability": "autopilot-refine", "shape": "solo", "why": "preview and refine"}
                self.set_briefs(refine, refine, approvals=approvals)
                self.set_interview({"legs": [refine], "execution_scope": scope, "entry_approvals": approvals})
                self.settle()
                route = json.loads(self.leg_routes()[0].read_text(encoding="utf-8"))
                self.assertEqual(route["entry_execution_scope"], scope)
                self.assertNotIn("preview-disposition", route.get("human_gates", []))
                self.assertFalse(any("preview-disposition" in node.get("inline_human_gates", [])
                                     for node in route["nodes"]))
                self.assertEqual(R.verify_route(route)["entry_execution_scope"], scope)

    def test_staged_refine_report_stops_at_review_and_complete_keeps_the_full_graph(self):
        approvals = [{"key": "preview", "leg": 0, "question": "route"}]
        refine = {"capability": "autopilot-refine", "shape": "staged",
                  "graph": ["review", "transaction"], "why": "review and refine"}
        self.set_briefs(refine, refine, approvals=approvals)
        self.set_interview({"legs": [refine], "execution_scope": "report", "entry_approvals": approvals})
        self.settle()
        record = self.record()
        self.assertEqual(record["decision"]["first_leg_compose"]["graph"], ["review"])
        route = json.loads(self.leg_routes()[0].read_text(encoding="utf-8"))
        self.assertNotIn("preview-disposition", route.get("human_gates", []))
        self.assertEqual(R.verify_route(route)["entry_execution_scope"], "report")


class SelectedLegTest(StartBase):
    def test_a164_2_a_direct_proposal_starts_one_first_leg_and_closes_the_frame_route(self):
        notice = io.StringIO()
        with contextlib.redirect_stderr(notice):
            result = self.settle()
        self.assertEqual((result["state"], result["required_action"]), ("inline", "execute-inline"), result)
        self.assertEqual(result["route_decision"]["selected"], ROUTE_LABELS[0])
        record = self.record()
        decision, first = record["decision"], record["first_leg"]
        self.assertEqual(decision["reason"], "")
        self.assertEqual(decision["proposal"]["legs"][0]["shape"], "direct")
        self.assertEqual(len(self.leg_routes()), 1)
        leg = json.loads(self.leg_routes()[0].read_text(encoding="utf-8"))
        self.assertEqual(first["route"]["route_id"], leg["route_id"])
        self.assertEqual(notice.getvalue().count("[경로]"), 1)
        self.assertIn("[경로] autopilot-code · direct(direct) inline", notice.getvalue())
        self.assertIn("route " + leg["route_id"], notice.getvalue())
        replay_notice = io.StringIO()
        with contextlib.redirect_stderr(replay_notice):
            replay = self.settle()
        self.assertEqual((replay["state"], replay["route_id"]), ("inline", leg["route_id"]))
        self.assertNotIn("[경로]", replay_notice.getvalue())
        self.assertEqual(leg["selection"]["shape"], "direct")
        self.assertEqual(leg["parent_cycle_id"], decision["frame_route"]["cycle_id"])
        self.assertEqual(leg["campaign_key"], "framed-key")
        self.assertEqual(leg["route_plan"], {"decision": self.record_path().relative_to(self.root).as_posix(),
                                              "digest": record["digest"], "index": 0})
        self.assertEqual(self.cli_calls, ["complete", "close"])
        self.assertTrue(R.outcome_path(self.path).exists())
        self.assertEqual(P.read_cycle_record(self.root, decision["frame_route"]["cycle_id"])["state"], "sealed")
        self.assertNotIn("next_leg", result)
        self.assertEqual(first["receipt_digest"][:7], "sha256:")
        rows = {row["node"]: row for row in decision["proposals"]}
        self.assertEqual({row["reason"] for row in rows.values()}, {"valid"})
        for node, row in rows.items():                       # the proposal's own text is kept, with its brief digest
            self.assertTrue(row["source"].startswith("route_proposal_v1:"), row)
            self.assertEqual(row["sha256"], RP.file_digest(self.output / "shards" / node / "direction-brief.md"))

    def test_a_staged_proposal_starts_one_registered_owner_and_returns_its_receipt(self):
        self.set_briefs(CODE_STAGED, CODE_STAGED)
        self.set_interview({"legs": [CODE_STAGED]})
        notice = io.StringIO()
        with contextlib.redirect_stderr(notice):
            result = self.settle()
        self.assertEqual(result["state"], "running", result)
        self.assertEqual(result["parent_next"], "end-turn")          # the owner's own directive, unchanged
        self.assertTrue(result["owner_started"])
        rows = self.registry_rows()
        self.assertEqual(len(rows), 1)
        (leg_path,) = self.leg_routes()
        leg = json.loads(leg_path.read_text(encoding="utf-8"))
        ids = [n["id"] for n in leg["nodes"]]
        self.assertEqual(ids, ["plan", "execute", "test", "report"])
        self.assertNotIn("frame-review", leg["human_gates"])
        self.assertFalse([n for n in leg["nodes"] if R._frame_node(n)])
        self.assertEqual(leg["composed"], True)
        self.assertEqual(leg["route_plan"]["index"], 0)
        self.assertEqual(len(self.leg_calls), 1)                     # one launch, of the owner
        self.assertIn("--prompt-text", self.leg_calls[0])
        task = self.leg_calls[0][self.leg_calls[0].index("--prompt-text") + 1]
        self.assertIn("## Agreed intent", task)
        self.assertIn("direction-brief.md", task)
        record = self.record()
        self.assertEqual(record["first_leg"]["route"]["route_id"], leg["route_id"])
        self.assertEqual(notice.getvalue().count("[경로]"), 1)
        self.assertIn("[경로] autopilot-code · staged(standard)", notice.getvalue())
        self.assertIn("route " + leg["route_id"], notice.getvalue())
        self.assertEqual(record["first_leg"]["start_receipt"]["owner_attempt_id"], result["owner_attempt_id"])
        self.assertEqual(self.cli_calls, ["complete", "close"])
        self.assertTrue(R.outcome_path(self.path).exists())

    def test_a_solo_proposal_starts_one_owner_that_runs_without_a_frame_pair(self):
        solo = {"capability": "autopilot-code", "shape": "solo", "why": "one registered worker"}
        self.set_briefs(solo, solo)
        self.set_interview({"legs": [solo]})
        result = self.settle()
        self.assertEqual(result["state"], "running", result)
        (leg_path,) = self.leg_routes()
        leg = json.loads(leg_path.read_text(encoding="utf-8"))
        self.assertEqual([n["id"] for n in leg["nodes"]], ["one-shot"])
        self.assertEqual(leg["selection"]["shape"], "solo")
        self.assertEqual((len(self.leg_calls), len(self.registry_rows())), (1, 1))
        self.assertNotIn("--route-node", self.leg_calls[0])           # no frame launch, only the owner
        self.assertEqual(self.cli_calls, ["complete", "close"])

    def test_a_repeated_start_after_the_ending_returns_the_legs_current_state_and_starts_nothing(self):
        # The owner still runs, so its current state is the receipt the first start stored.
        self.set_briefs(CODE_STAGED, CODE_STAGED)
        self.set_interview({"legs": [CODE_STAGED]})
        first = self.settle()
        from dispatch_notice_state import closed_outcome
        closed = closed_outcome(Path(self.path), self.route)
        again = self.settle(closed=closed)
        self.assertEqual({k: v for k, v in again.items() if k != "route_decision"},
                         {k: v for k, v in first.items() if k != "route_decision"})
        self.assertEqual(len(self.leg_calls), 1)
        self.assertEqual(len(self.leg_routes()), 1)
        self.assertEqual(len(self.registry_rows()), 1)
        self.assertEqual(self.cli_calls, ["complete", "close"])

    def test_d11_a_copy_of_the_review_display_legs_selects_the_brief_proposal(self):
        # The legs route_proposal_review shows resolve what the brief left out (mode, intensity).
        legs = [dict(CODE_STAGED, graph=["plan", "execute", "test"]),
                {"capability": "autopilot-refine", "shape": "staged", "graph": ["review", "transaction"], "why": "docs"}]
        self.set_briefs(legs, legs)
        row = W._proposal_review(self.route, self.path, self.jobs)["proposals"][0]
        keys = ("capability", "mode", "shape", "graph", "intensity")
        self.assertNotEqual(row["legs"], [{k: leg[k] for k in keys} for leg in row["proposal"]["legs"]])
        self.assertEqual((row["legs"][1]["mode"], row["legs"][0]["intensity"]), ("default", "standard"))
        self.set_interview({"legs": [{**shown, "why": own["why"]} for shown, own in zip(row["legs"], row["proposal"]["legs"])]})
        result = self.settle()
        decision = self.record()["decision"]
        self.assertEqual((decision["selected"], decision["reason"]), (ROUTE_LABELS[0], ""))   # HEAD: ('none', 'proposal-not-verified')
        self.assertEqual(result["route_decision"]["selected"], ROUTE_LABELS[0], result)
        self.assertEqual(decision["proposal"], row["proposal"])          # the brief's own proposal is recorded, not the copy
        self.assertEqual(len(self.leg_routes()), 1)

    def test_a164_3_two_different_proposals_each_select_their_own_route(self):
        for label_index, leg in ((0, DIRECT), (1, CODE_STAGED)):
            with self.subTest(route=leg["shape"]):
                self.tearDown()
                self.setUp()
                self.set_briefs(DIRECT, CODE_STAGED)
                self.interview = {
                    "schema": FI.SCHEMA, "route_id": self.route["route_id"], "round": 1,
                    "questions": [question("route", kind="choice", labels=("먼저 빠르게", "단계대로", "다르게 할게요"))],
                    "route_proposals": {"question": "route", "by_option": {
                        "먼저 빠르게": {"summary": "a", "legs": [DIRECT], "entry_approvals": []},
                        "단계대로": {"summary": "b", "legs": [CODE_STAGED], "entry_approvals": []}}}}
                self.answers["answers"] = {"route": {"choice": label_index, "note": ""}}
                self.settle()
                record = self.record()
                self.assertEqual(record["decision"]["selected"], ("먼저 빠르게", "단계대로")[label_index])
                leg = json.loads(self.leg_routes()[0].read_text(encoding="utf-8"))
                self.assertEqual(leg["selection"]["shape"], "direct" if label_index == 0 else "staged")
                self.assertEqual(len(self.leg_routes()), 1)


class NoneEndingTest(StartBase):
    def assert_none(self, reason_prefix, result=None):
        result = result or self.settle()
        self.assertEqual((result["state"], result["required_action"], result["selected"]),
                         ("completed", "compose-route", "none"), result)
        self.assertTrue(result["decision_reason"].startswith(reason_prefix), result["decision_reason"])
        self.assertEqual(self.leg_routes(), [])
        self.assertEqual(self.registry_rows(), [])
        self.assertEqual(self.leg_calls, [])
        self.assertEqual(self.cli_calls, ["complete", "close"])
        self.assertTrue(R.outcome_path(self.path).exists())
        self.assertEqual(P.read_cycle_record(self.root, self.record()["decision"]["frame_route"]["cycle_id"])["state"], "sealed")
        for forbidden in ("next_leg", "parent_next", "parent_next_command"):
            self.assertNotIn(forbidden, result)
        self.assertNotIn("first_leg", self.record())
        return result

    def test_a164_4_an_interview_without_route_proposals_ends_as_before(self):
        del self.interview["route_proposals"]
        self.assert_none("proposal-not-read")

    def test_a164_4_a_declined_route_question_selects_nothing(self):
        self.set_interview({"legs": [DIRECT]}, route_choice=1)
        self.assert_none("route-declined")

    def test_a164_4_an_off_menu_answer_selects_nothing(self):
        self.set_interview({"legs": [DIRECT]}, route_choice="none")
        self.assert_none("route-off-menu")

    def test_a164_4_a_proposal_no_brief_made_is_not_started(self):
        self.set_interview({"legs": [CODE_STAGED]})            # the briefs proposed a direct step
        self.assert_none("proposal-not-verified")

    def test_d11_a_copy_that_differs_from_the_display_in_a_named_field_is_still_not_verified(self):
        legs = [dict(CODE_STAGED, graph=["plan", "execute", "test"]),
                {"capability": "autopilot-refine", "shape": "staged", "graph": ["review", "transaction"], "why": "docs"}]
        changes = {"capability": (1, {"capability": "autopilot-draft"}), "mode": (0, {"mode": "debug"}),
                   "refine-mode": (1, {"mode": "dev"}), "shape": (1, {"shape": "solo", "graph": None}),
                   "graph": (0, {"graph": ["plan", "execute"]}), "intensity": (0, {"intensity": "strong"})}
        for label, (index, change) in changes.items():
            with self.subTest(label):
                self.tearDown()
                self.setUp()
                self.set_briefs(legs, legs)
                shown = [dict(leg) for leg in W._proposal_review(self.route, self.path, self.jobs)["proposals"][0]["legs"]]
                shown[index].update(change)
                self.set_interview({"legs": shown})
                self.assert_none("proposal-not-verified")

    def test_a164_4_briefs_with_no_valid_proposal_end_with_each_reason_recorded(self):
        self.set_briefs(brief([DIRECT], section=False), "## 8. 경로 조립 제안\n\nnothing fenced\n")
        result = self.assert_none("proposal-not-verified")
        rows = {row["node"]: row for row in self.record()["decision"]["proposals"]}
        self.assertEqual(rows["frame"]["reason"], "block-missing")          # section 8 under another title
        self.assertEqual(rows["frame-alternative"]["reason"], "block-missing")

    def test_an_invalid_later_leg_is_cut_and_named_and_a_copy_with_it_is_not_verified(self):
        bad = {"capability": "autopilot-code", "shape": "staged", "graph": ["execute", "no-such-stage"]}
        self.set_briefs([DIRECT, bad], DIRECT)
        self.set_interview({"legs": [DIRECT, bad]})
        self.assert_none("proposal-not-verified")
        rows = {row["node"]: row for row in self.record()["decision"]["proposals"]}
        self.assertEqual((rows["frame"]["reason"], len(rows["frame"]["proposal"]["legs"])), ("valid", 1))
        self.assertRegex(rows["frame"]["read_notes"][0], r"^leg-invalid:1:compose-graph-unknown-node")
        self.assertEqual(rows["frame-alternative"]["reason"], "valid")


class ApprovalTest(StartBase):
    """Start approvals: taken in the one interview, valid only for the leg and parts the user saw."""

    def lab(self, *, approval=True, approval_choice=None, labels=("예, 지금 시작", "아니요, 나중에"), approves=0):
        approvals = [{"key": "full-run", "leg": 0, "question": "run-ok"}] if approval else []
        self.set_briefs(LAB_SETUP, LAB_SETUP, approvals=approvals)
        self.set_interview({"legs": [LAB_SETUP], "entry_approvals": approvals},
                           extra_questions=[question("run-ok", labels=labels, approves=approves)] if approval else (),
                           approval_choice=approval_choice)

    def test_a164_10_a_yes_to_the_approval_starts_the_leg_and_records_what_was_approved(self):
        self.lab()
        result = self.settle()
        self.assertEqual(result["state"], "running", result)
        given = self.record()["decision"]["approvals"]["given"]
        self.assertEqual(given, [{"key": "full-run", "leg": 0, "question": "run-ok", "label": "예, 지금 시작",
                                  "accepted": True, "parts": ["autopilot-lab:full-run"]}])
        task = self.leg_calls[0][self.leg_calls[0].index("--prompt-text") + 1]
        self.assertIn("- full-run for leg 0 (autopilot-lab:full-run): approved", task)
        self.assertEqual(len(self.leg_routes()), 1)

    def test_a_korean_approval_question_id_is_normalized_and_the_start_goes_through(self):
        approvals = [{"key": "full-run", "leg": 0, "question": "문서시작"}]
        self.set_briefs(LAB_SETUP, LAB_SETUP, approvals=approvals)
        normalized = [{"key": "full-run", "leg": 0, "question": "full-run-leg0"}]
        self.set_interview({"legs": [LAB_SETUP], "entry_approvals": normalized},
                           extra_questions=[question("full-run-leg0", labels=("예, 지금 시작", "아니요, 나중에"), approves=0)])
        review = W._proposal_review(self.route, self.path, self.jobs)
        for row in review["proposals"]:
            self.assertEqual(row["reason"], "valid", row)
            self.assertEqual(row["proposal"]["entry_approvals"], normalized)
            self.assertEqual(row["question_renames"], [{**normalized[0], "original": "문서시작"}])
        result = self.settle()
        self.assertEqual(result["state"], "running", result)             # not none(schema-invalid)
        decision = self.record()["decision"]
        self.assertEqual(decision["approvals"]["given"][0]["question"], "full-run-leg0")
        self.assertTrue(decision["approvals"]["given"][0]["accepted"])
        self.assertTrue(all("문서시작" in row["source"] for row in decision["proposals"]), decision["proposals"])

    def test_a164_10_declining_the_approval_is_a_valid_answer_that_starts_nothing(self):
        self.lab(approval_choice=1)
        NoneEndingTest.assert_none(self, "approval-missing:full-run")
        given = self.record()["decision"]["approvals"] if False else None
        self.assertEqual(self.record()["decision"]["reason"], "approval-missing:full-run")

    def test_an_approving_option_that_is_second_starts_the_leg_when_chosen(self):
        self.lab(labels=("아니요, 나중에", "예, 지금 시작"), approves=1, approval_choice=1)
        result = self.settle()
        self.assertEqual(result["state"], "running", result)
        given = self.record()["decision"]["approvals"]["given"]
        self.assertEqual([(row["label"], row["accepted"]) for row in given], [("예, 지금 시작", True)])
        self.assertEqual(len(self.leg_routes()), 1)

    def test_a_declining_option_that_is_first_does_not_approve_when_chosen(self):
        self.lab(labels=("아니요, 나중에", "예, 지금 시작"), approves=1, approval_choice=0)
        NoneEndingTest.assert_none(self, "approval-missing:full-run")
        self.assertEqual(self.record()["decision"]["reason"], "approval-missing:full-run")
        self.assertEqual(self.leg_calls, [])

    def test_a_question_with_no_marked_option_or_two_marked_options_starts_nothing(self):
        for label, marks in (("none marked", None), ("both marked", "both")):
            with self.subTest(label):
                self.tearDown()
                self.setUp()
                self.lab(approves=marks)
                if marks == "both":
                    for option in self.interview["questions"][1]["options"]:
                        option["approves"] = True
                self.assertTrue(any("approves" in e for e in FI.validate(self.interview)))
                NoneEndingTest.assert_none(self, "approval-missing:full-run")
                self.assertEqual(self.leg_calls, [])

    def test_a_supervisor_yes_starts_a_full_run_and_holds_a_preview_for_the_person(self):
        self.lab()
        self.answers["actor_kind"] = "supervisor"
        self.assertEqual(self.settle()["state"], "running")
        given, = self.record()["decision"]["approvals"]["given"]
        self.assertEqual((given["key"], given["accepted"]), ("full-run", True))
        route = json.loads(self.leg_routes()[0].read_text(encoding="utf-8"))
        self.assertEqual(route["entry_execution_scope"], "complete")
        self.tearDown()
        self.setUp()
        approvals = [{"key": "preview", "leg": 0, "question": "route"}]
        refine = {"capability": "autopilot-refine", "shape": "solo", "why": "preview and refine"}
        self.set_briefs(refine, refine, approvals=approvals)
        self.set_interview({"legs": [refine], "entry_approvals": approvals})
        self.answers["actor_kind"] = "supervisor"
        self.assertEqual(self.settle()["state"], "running")
        given, = self.record()["decision"]["approvals"]["given"]
        self.assertEqual((given["key"], given["accepted"], given["held_for_person"]), ("preview", False, True))
        route = json.loads(self.leg_routes()[0].read_text(encoding="utf-8"))
        self.assertNotIn("entry_execution_scope", route)                 # the preview keeps its own gate
        self.assertTrue("preview-disposition" in route.get("human_gates", []) or any(
            "preview-disposition" in node.get("inline_human_gates", []) for node in route["nodes"]), route)
        task = self.leg_calls[0][self.leg_calls[0].index("--prompt-text") + 1]
        self.assertIn("(autopilot-refine:transaction): held for the person at its own gate", task)

    def test_a_leg_with_an_approval_part_and_no_approval_asked_ends_as_none(self):
        self.lab(approval=False)
        NoneEndingTest.assert_none(self, "approval-missing:full-run")

    def test_an_approval_for_a_later_leg_does_not_unlock_the_first_leg(self):
        approvals = [{"key": "full-run", "leg": 1, "question": "run-ok"}]
        self.set_briefs([DIRECT, LAB_SETUP], [DIRECT, LAB_SETUP], approvals=approvals)
        self.set_interview({"legs": [DIRECT, LAB_SETUP], "entry_approvals": approvals},
                           extra_questions=[question("run-ok", labels=("예", "아니요"), approves=0)])
        result = self.settle()                     # leg 0 is direct: no approval part, so it starts
        self.assertEqual(result["state"], "inline", result)
        given = self.record()["decision"]["approvals"]["given"]
        self.assertEqual([(row["leg"], row["accepted"]) for row in given], [(1, True)])

    def test_the_intent_shows_the_approval_with_the_parts_the_user_was_shown(self):
        self.lab()
        interview, answers = self.interview, self.answers
        scope = W._approval_scope(interview, answers)
        self.assertEqual(scope, {("full-run", 0): ["autopilot-lab:full-run"]})
        text = FI.render_intent(interview, answers, now="2026-10-01", approval_scope=scope)
        self.assertIn("## Route", text)
        self.assertIn("- full-run for leg 0 (autopilot-lab:full-run) — question `run-ok`: approved", text)
        self.assertEqual(text, FI.render_intent(interview, answers, now="2026-10-01", approval_scope=scope))
        self.assertIn("(starts now)", text)
        answers["answers"]["run-ok"]["choice"] = 1
        self.assertIn("question `run-ok`: declined", FI.render_intent(interview, answers, now="2026-10-01", approval_scope=scope))


class RuntimeBuiltInterviewTest(StartBase):
    """The runtime builds the route question and the start-approval questions from the validated
    proposals; the session only words them, and the marks become `route_proposals` on submission."""

    def built(self):
        root, record, output = W._framed_cycle(self.route)
        return W._interview_questions(W._proposal_rows(self.route, self.jobs, root, record, output))

    def worded(self, questions, *extra):
        for number, item in enumerate(questions):
            item.update(topic=f"topic {number}", question=f"Question {number}?", why="Only you can say.")
            for at, option in enumerate(item["options"]):
                option.update(label=f"Option {at}", means=f"Choosing option {at}.")
        return {"schema": FI.SCHEMA, "route_id": self.route["route_id"], "round": 1, "summary": "-",
                "understanding": "Do the work the request names.",
                "brief": {"problem": "p", "outcome": "o", "affected": "a", "constraints": "c", "open": ""},
                "questions": questions + list(extra)}

    def submit(self, interview, choices, *, actor_kind="user"):
        derived, errors = W._marked_route_proposals(self.route, self.jobs, interview)
        self.assertEqual(errors, [])
        self.assertEqual(FI.validate(derived), [])
        self.interview = derived
        self.answers = {"schema": FI.ANSWERS_SCHEMA, "route_id": self.route["route_id"], "round": 1,
                        "actor_kind": actor_kind, "understanding_confirmed": True, "correction": "",
                        "answers": {qid: {"choice": choice, "note": ""} for qid, choice in choices.items()}}
        return derived

    def test_equal_proposals_get_one_yes_no_route_question_marked_with_the_first_brief(self):
        questions = self.built()
        self.assertEqual([(q["id"], q["kind"]) for q in questions], [(FI.ROUTE_QUESTION_ID, "yes-no")])
        self.assertEqual([o.get(FI.PROPOSAL_MARK) for o in questions[0]["options"]], ["frame", None])
        self.assertTrue(all(q["question"] == "" and q["why"] == "" for q in questions))   # wording is the session's

    def test_different_proposals_get_one_choice_per_brief_and_each_selects_its_own_route(self):
        for choice, expected in ((0, "inline"), (1, "running")):
            with self.subTest(choice=choice):
                self.tearDown()
                self.setUp()
                self.set_briefs(DIRECT, CODE_STAGED)
                questions = self.built()
                self.assertEqual(questions[0]["kind"], "choice")
                self.assertEqual([o[FI.PROPOSAL_MARK] for o in questions[0]["options"]], F.FRAME_IDS)
                derived = self.submit(self.worded(questions), {FI.ROUTE_QUESTION_ID: choice})
                self.assertEqual(sorted(derived["route_proposals"]["by_option"]), ["Option 0", "Option 1"])
                result = self.settle()
                self.assertEqual(result["state"], expected, result)
                self.assertEqual(self.record()["decision"]["selected"], f"Option {choice}")

    def test_a_start_approval_gets_its_own_question_and_a_yes_starts_the_leg(self):
        self.set_briefs(LAB_SETUP, LAB_SETUP)                    # the briefs name no approval question
        questions = self.built()
        self.assertEqual([q["id"] for q in questions], [FI.ROUTE_QUESTION_ID, "full-run-leg0"])
        self.assertEqual([o.get("approves") for o in questions[1]["options"]], [True, None])
        derived = self.submit(self.worded(questions), {FI.ROUTE_QUESTION_ID: 0, "full-run-leg0": 0})
        self.assertEqual(derived["route_proposals"]["by_option"]["Option 0"]["entry_approvals"],
                         [{"key": "full-run", "leg": 0, "question": "full-run-leg0"}])
        result = self.settle()
        self.assertEqual(result["state"], "running", result)
        given = self.record()["decision"]["approvals"]["given"]
        self.assertEqual([(row["question"], row["accepted"]) for row in given], [("full-run-leg0", True)])

    def test_a_later_legs_approval_question_may_be_left_out_and_that_leg_keeps_its_gate(self):
        self.set_briefs([DIRECT, LAB_SETUP], [DIRECT, LAB_SETUP])
        questions = self.built()
        self.assertEqual([q["id"] for q in questions], [FI.ROUTE_QUESTION_ID, "full-run-leg1"])
        derived = self.submit(self.worded(questions[:1]), {FI.ROUTE_QUESTION_ID: 0})
        self.assertEqual(derived["route_proposals"]["by_option"]["Option 0"]["entry_approvals"], [])
        self.assertEqual(self.settle()["state"], "inline")
        self.assertEqual(self.record()["decision"]["approvals"]["given"], [])

    def test_marks_that_name_no_valid_proposal_or_sit_on_two_questions_are_reported(self):
        self.set_briefs(DIRECT, "## 8. 경로 조립 제안\n\nnone\n")
        questions = self.built()
        questions[0]["options"][1][FI.PROPOSAL_MARK] = "frame-alternative"
        _, errors = W._marked_route_proposals(self.route, self.jobs, self.worded(questions))
        self.assertEqual(errors, [f"{FI.ROUTE_QUESTION_ID}.options[1].proposal: no valid proposal from 'frame-alternative'"])
        twice = self.worded(self.built(), dict(question("again"), options=[
            {"label": "a", "means": "a", FI.PROPOSAL_MARK: "frame"}, {"label": "b", "means": "b"}]))
        self.assertEqual(W._marked_route_proposals(self.route, self.jobs, twice)[1],
                         ["questions: proposal marks belong to one route question"])

    def test_an_interview_that_maps_its_own_proposals_or_has_no_marks_is_unchanged(self):
        own = copy.deepcopy(self.interview)
        self.assertEqual(W._marked_route_proposals(self.route, self.jobs, own), (own, []))
        plain = self.worded([question("scope")])
        self.assertEqual(W._marked_route_proposals(self.route, self.jobs, plain), (plain, []))


class LegWorktreeTest(StartBase):
    """A source-changing leg runs in its own worktree, prepared from the frame's checkout."""

    def test_an_existing_linked_worktree_is_used_for_validation_launch_and_replay(self):
        repo = self.root.parent / "source"
        worktree = self.root.parent / "source-wt" / "chosen folder"
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        subprocess.run(["git", "-C", str(repo), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.test",
                        "commit", "-q", "--allow-empty", "-m", "base"], check=True)
        subprocess.run(["git", "-C", str(repo), "worktree", "add", "-q", "-b", "chosen", str(worktree)], check=True)
        dirty = worktree / "uncommitted.py"
        dirty.write_text("pending source\n", encoding="utf-8")
        chosen = {**CODE_STAGED, "cwd": str(worktree)}
        self.set_briefs(chosen, chosen)
        self.set_interview({"legs": [chosen]})
        probed = []

        def readiness(frame_route, jobs):
            probed.append(frame_route["cwd"])
            return {"tuples": [{**T.nested(parent, child), "checked_worktree": frame_route["cwd"]}
                               for parent in ("claude", "codex", "opencode")
                               for child in ("claude", "codex", "opencode")],
                    "candidates": T.registered_headless()["candidates"]}

        with mock.patch.object(R, "proposal_readiness", side_effect=readiness):
            result = self.settle()
            replay = self.settle()
        self.assertTrue(result["owner_started"], result)
        self.assertTrue(probed)
        self.assertEqual(set(probed), {str(worktree)})
        (leg_path,) = self.leg_routes()
        leg = json.loads(leg_path.read_text(encoding="utf-8"))
        self.assertEqual(leg["cwd"], str(worktree))
        self.assertEqual(R.verify_route(copy.deepcopy(leg), worktree)["cwd"], str(worktree))
        self.assertEqual(replay["route_id"], leg["route_id"])
        self.assertEqual(len(self.leg_calls), 1)
        command = self.leg_calls[0]
        self.assertEqual(Path(command[command.index("--route-evidence") + 1]), leg_path)
        self.assertEqual({row["checked_worktree"] for row in leg["dispatch_evidence"]["tuples"]}, {str(worktree)})
        self.assertEqual(dirty.read_text(encoding="utf-8"), "pending source\n")
        self.assertEqual(self.record()["decision"]["proposal"]["legs"][0]["cwd"], str(worktree))

    def test_a_later_leg_uses_its_own_folder_and_relative_paths_use_the_original_frame(self):
        one, two = self.root.parent / "one", self.root.parent / "two"
        one.mkdir()
        two.mkdir()
        relative = os.path.relpath(two, self.route["cwd"])
        first, second = {**DIRECT, "cwd": str(one)}, {**DIRECT, "cwd": relative}
        self.set_briefs([first, second], [first, second])
        self.set_interview({"legs": [first, second]})
        self.assertEqual(self.settle()["state"], "inline")
        (first_path,) = self.leg_routes()
        first_route = json.loads(first_path.read_text(encoding="utf-8"))
        cycle = self.finish_leg(first_route, first_path)
        projected = RP.project_next_leg(first_route, cycle)
        argv = shlex.split(projected["compose_command"])
        self.assertEqual(argv[argv.index("--cwd") + 1], str(two))
        result = W.start_work(first_route, first_path, self.jobs, run=self.fake_run)
        second_route = json.loads(Path(result["route_file"]).read_text(encoding="utf-8"))
        self.assertEqual(second_route["cwd"], str(two))
        self.assertEqual(second_route["route_plan"]["index"], 1)

    def test_the_first_source_changing_leg_is_sealed_in_the_prepared_worktree_with_its_own_probe(self):
        with tempfile.TemporaryDirectory() as tmp:
            worktree = Path(tmp) / "repo-wt" / "framed"
            subprocess.run(["git", "init", "-q", str(worktree)], check=True)
            probed = []

            def readiness(frame_route, jobs):
                probed.append(frame_route["cwd"])
                row = {**T.nested("claude", "codex"), "checked_worktree": str(Path(frame_route["cwd"]).resolve())}
                return {"tuples": [row], "candidates": T.registered_headless()["candidates"]}
            prepared = {"state": "created", "path": str(worktree), "cwd": str(worktree), "branch": "framed",
                        "base": "origin/main"}
            self.set_briefs(CODE_STAGED, CODE_STAGED)
            self.set_interview({"legs": [CODE_STAGED]})
            with mock.patch.object(R, "proposal_readiness", side_effect=readiness), \
                    mock.patch.object(R, "_isolates_worktree", return_value=True), \
                    mock.patch.object(R, "prepare_isolated_worktree", return_value=prepared) as prepare:
                result = self.settle()
            self.assertTrue(result["owner_started"], result)
            (leg_path,) = self.leg_routes()
            leg = json.loads(leg_path.read_text(encoding="utf-8"))
            self.assertEqual(leg["cwd"], str(worktree))
            prepare.assert_called_once_with(
                self.route["cwd"], self.route["slug"], artifact_root=self.route["artifact_root"],
                preparation_key=R._route_plan_preparation_key(
                    RP.read_route_plan(f"{self.record_path()}#0", self.root)), explicitly_approved=False)
            self.assertIn(str(worktree), probed)                 # the worktree was probed, not the frame's checkout


class SourceRepoStartBase(StartBase):
    """A framed route over a real primary checkout with a bare origin that moved ahead of it."""

    def setUp(self):
        self.source_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.source_tmp.cleanup)
        base = Path(self.source_tmp.name)
        self.primary = base / "repo"
        self.origin = base / "origin.git"
        self.writer = base / "writer"
        git = lambda cwd, *args: subprocess.run(
            ["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True).stdout.strip()
        git(base, "init", "-q", "--bare", "-b", "main", str(self.origin))
        git(base, "init", "-q", "-b", "main", str(self.primary))
        git(self.primary, "config", "user.name", "Fixture")
        git(self.primary, "config", "user.email", "fixture@example.test")
        (self.primary / "base.txt").write_text("base\n", encoding="utf-8")
        git(self.primary, "add", "base.txt")
        git(self.primary, "commit", "-qm", "base")
        git(self.primary, "remote", "add", "origin", str(self.origin))
        git(self.primary, "push", "-q", "-u", "origin", "main")
        git(self.primary, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.test",
            "commit", "--allow-empty", "-qm", "primary-only")
        git(base, "clone", "-q", str(self.origin), str(self.writer))
        git(self.writer, "config", "user.name", "Fixture")
        git(self.writer, "config", "user.email", "fixture@example.test")
        (self.writer / "remote-tip.txt").write_text("remote tip\n", encoding="utf-8")
        git(self.writer, "add", "remote-tip.txt")
        git(self.writer, "commit", "-qm", "remote tip")
        git(self.writer, "push", "-q", "origin", "main")
        self.remote_sha = git(self.writer, "rev-parse", "HEAD")
        (self.primary / "base.txt").write_text("staged\n", encoding="utf-8")
        git(self.primary, "add", "base.txt")
        (self.primary / "base.txt").write_text("unstaged\n", encoding="utf-8")
        (self.primary / "untracked.txt").write_text("preserve\n", encoding="utf-8")
        self.probe_cwds = []
        self.primary_before = self.primary_fingerprint()
        super().setUp()
        self.set_briefs(CODE_STAGED, CODE_STAGED)
        self.set_interview({"legs": [CODE_STAGED]})

    def compose(self, **kwargs):
        kwargs["cwd"] = self.primary
        kwargs["dispatch_evidence"] = {"tuples": [
            {**T.nested("claude", "codex"), "checked_worktree": str(self.primary.resolve())}]}
        return super().compose(**kwargs)

    def readiness(self, frame_route, jobs):
        cwd = str(Path(frame_route["cwd"]).resolve())
        self.probe_cwds.append(cwd)
        row = {**T.nested("claude", "codex"), "checked_worktree": cwd}
        return {"tuples": [row], "candidates": T.registered_headless()["candidates"]}

    def primary_fingerprint(self):
        git = lambda *args: subprocess.run(
            ["git", "-C", str(self.primary), *args], check=True, capture_output=True, text=True).stdout.strip()
        return (git("rev-parse", "HEAD"), git("branch", "--show-current"), git("write-tree"),
                git("diff", "--binary"), git("diff", "--cached", "--binary"), git("status", "--porcelain"),
                (self.primary / "base.txt").read_bytes(), (self.primary / "untracked.txt").read_bytes())


class FirstLegPreparationFailureRetryTest(SourceRepoStartBase):
    """Preparation failures cross the real framed decision/first-leg boundary and retry in place."""

    def fail_then_retry(self, failure):
        before = self.primary_fingerprint()
        initial_attempts, initial_routes = len(self.registry_rows()), len(self.leg_routes())
        real = R._worktree_git
        injected = {"done": False}

        def fail_once(cwd, *args):
            is_target = (failure == "fetch" and args[:1] == ("fetch",)
                         or failure == "add" and args[:2] == ("worktree", "add"))
            if is_target and not injected["done"]:
                injected["done"] = True
                return subprocess.CompletedProcess(["git", *args], 128, "", f"injected {failure} failure")
            return real(cwd, *args)

        with patch_split(R, "_worktree_git", side_effect=fail_once):
            failed = self.settle()
        self.assertTrue(injected["done"])
        self.assertEqual(failed["reason"], "first-leg-preparation-failed", failed)
        self.assertEqual(self.primary_fingerprint(), before)
        self.assertEqual((len(self.leg_routes()), len(self.registry_rows()), len(self.leg_calls)),
                         (initial_routes, initial_attempts, 0))
        # The frame's own earlier probe remains in the primary; the failed source start adds no
        # first-leg probe there and no execution route can consume that evidence.
        self.assertEqual(self.probe_cwds, [str(self.primary.resolve())])
        decision = copy.deepcopy(self.record()["decision"])
        self.assertNotIn("snapshot", self.record().get("first_leg", {}))

        with mock.patch.object(W, "_recorded_interview", side_effect=AssertionError("same start re-interviewed")):
            retried = self.settle()
        self.assertTrue(retried["owner_started"], retried)
        self.assertEqual(self.record()["decision"], decision)
        self.assertEqual(self.primary_fingerprint(), before)
        self.assertEqual((len(self.leg_routes()), len(self.registry_rows()), len(self.leg_calls)),
                         (initial_routes + 1, initial_attempts + 1, 1))
        route_path, = self.leg_routes()
        route = json.loads(route_path.read_text(encoding="utf-8"))
        expected = self.primary.parent / "repo-wt" / self.route["slug"]
        self.assertEqual(route["cwd"], str(expected.resolve()))
        self.assertEqual(self.probe_cwds, [str(self.primary.resolve()), route["cwd"]])
        self.assertEqual({row["checked_worktree"] for row in route["dispatch_evidence"]["tuples"]},
                         {route["cwd"]})
        git = lambda *args: subprocess.run(
            ["git", "-C", route["cwd"], *args], check=True, capture_output=True, text=True).stdout.strip()
        self.assertEqual(git("rev-parse", "HEAD"), self.remote_sha)
        records = list((self.root / ".runtime" / "worktree-preparation").glob("*.json"))
        self.assertEqual(len(records), 1)
        self.assertEqual(json.loads(records[0].read_text(encoding="utf-8"))["state"], "ready")

    def test_first_leg_fetch_failure_keeps_decision_and_same_start_retries_once(self):
        self.fail_then_retry("fetch")

    def test_first_leg_worktree_add_failure_keeps_decision_and_same_start_retries_once(self):
        self.fail_then_retry("add")

    def first_source_leg_and_projection(self):
        self.set_briefs([DIRECT, CODE_STAGED], [DIRECT, CODE_STAGED])
        self.set_interview({"legs": [DIRECT, CODE_STAGED]})
        first_start = self.settle()
        self.assertEqual(first_start["state"], "inline", first_start)
        first_path, = self.leg_routes()
        first = json.loads(first_path.read_text(encoding="utf-8"))
        cycle = self.finish_leg(first, first_path)
        projection = RP.project_next_leg(first, cycle)
        self.assertIsNotNone(projection)
        return first, first_path, projection

    def test_automatic_source_successor_reuses_first_leg_allocation(self):
        first, _, projection = self.first_source_leg_and_projection()
        linked = first["cwd"]
        argv = shlex.split(projection["compose_command"])
        self.assertEqual(argv[argv.index("--cwd") + 1], linked)
        real = R._worktree_git

        def no_new_fetch(cwd, *args):
            self.assertNotIn(args[:1], (("ls-remote",), ("fetch",)))
            return real(cwd, *args)

        from dispatch_notice_state import closed_outcome
        with patch_split(R, "_worktree_git", side_effect=no_new_fetch), \
                mock.patch.object(W, "_recorded_interview", side_effect=AssertionError("successor re-interviewed")):
            advanced = self.settle(closed=closed_outcome(Path(self.path), self.route))
        self.assertEqual(advanced["plan_advanced"]["leg"], 1)
        routes = [json.loads(path.read_text(encoding="utf-8")) for path in self.leg_routes()]
        second, = [route for route in routes if route["route_plan"]["index"] == 1]
        self.assertEqual(second["cwd"], linked)
        self.assertEqual({row["checked_worktree"] for row in second["dispatch_evidence"]["tuples"]}, {linked})
        records = list((self.root / ".runtime" / "worktree-preparation").glob("*.json"))
        self.assertEqual(len(records), 1)
        self.assertEqual(json.loads(records[0].read_text(encoding="utf-8"))["path"], linked)

    def test_printed_source_next_leg_command_keeps_preparation_and_canonical_root(self):
        first, _, projection = self.first_source_leg_and_projection()
        argv = shlex.split(projection["compose_command"])
        self.assertIn("--start", argv)
        self.assertEqual(argv[argv.index("--cwd") + 1], first["cwd"])
        cli_argv = ["capability-route.py", *argv[2:]]
        probed = []

        def readiness(cwd, jobs, parent_harness, children, **kwargs):
            final = str(Path(cwd).resolve())
            probed.append(final)
            row = {**T.nested(parent_harness, "codex"), "checked_worktree": final}
            return {"tuples": [row], "candidates": T.registered_headless()["candidates"]}

        starts = []

        def start(route, path, jobs):
            starts.append((route, Path(path)))
            return {"state": "running", "owner_attempt_id": "printed-next-leg", "owner_started": True,
                    "launches": [{"receipt": "registered=1 started=1 child_spawned=1"}]}

        real = R._worktree_git

        def no_new_fetch(cwd, *args):
            self.assertNotIn(args[:1], (("ls-remote",), ("fetch",)))
            return real(cwd, *args)

        with patch_split(R, "_compose_readiness", side_effect=readiness), \
                patch_split(R, "_worktree_git", side_effect=no_new_fetch), \
                mock.patch.object(W, "start_work", side_effect=start), mock.patch.object(sys, "argv", cli_argv):
            self.assertEqual(R.main(), 0)
        self.assertEqual(len(starts), 1)
        route, _ = starts[0]
        self.assertEqual(route["cwd"], first["cwd"])
        self.assertEqual(route["artifact_root"], str(self.root.resolve()))
        self.assertEqual(probed, [first["cwd"]])
        records = list((self.root / ".runtime" / "worktree-preparation").glob("*.json"))
        self.assertEqual(len(records), 1)
        self.assertEqual(json.loads(records[0].read_text(encoding="utf-8"))["path"], first["cwd"])


class FrameAllocationInheritanceTest(SourceRepoStartBase):
    """The frame's own start allocation is its first source leg's checkout: one fetch, one worktree."""

    def compose(self, **kwargs):
        if not hasattr(self, "frame_prepared"):
            self.frame_prepared = R.prepare_isolated_worktree(
                self.primary, "framed-fixture", artifact_root=self.root,
                preparation_key={"fixture": "framed-start"}, framed_start=True)
        cwd = self.frame_prepared["cwd"]
        kwargs["cwd"] = Path(cwd)
        kwargs["dispatch_evidence"] = {"tuples": [{**T.nested("claude", "codex"), "checked_worktree": cwd}]}
        return R._seal_framed_worktree(super(SourceRepoStartBase, self).compose(**kwargs), self.frame_prepared)

    def git(self, cwd, *args):
        return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True,
                              text=True).stdout.strip()

    def test_the_frame_reads_the_fetched_remote_default_and_its_first_source_leg_stays_there(self):
        expected = (self.primary.parent / "repo-wt" / "framed-fixture").resolve()
        self.assertEqual(self.frame_prepared["state"], "created")
        self.assertEqual((self.route["cwd"], self.route["source_commit"]), (str(expected), self.remote_sha))
        worktrees = self.git(self.primary, "worktree", "list", "--porcelain")
        real = R._worktree_git

        def no_new_allocation(cwd, *args):
            self.assertNotIn(args[:1], (("ls-remote",), ("fetch",)))
            self.assertNotEqual(args[:2], ("worktree", "add"))
            return real(cwd, *args)

        with patch_split(R, "_worktree_git", side_effect=no_new_allocation):
            result = self.settle()
        self.assertTrue(result["owner_started"], result)
        self.assertEqual(R.prepare_isolated_worktree.call_count, 0)
        leg_path, = self.leg_routes()
        leg = json.loads(leg_path.read_text(encoding="utf-8"))
        self.assertEqual((leg["cwd"], leg["source_commit"]), (str(expected), self.remote_sha))
        self.assertEqual((leg["framed_worktree"]["state"], leg["framed_worktree"]["from_route_id"]),
                         ("inherited", self.route["route_id"]))
        self.assertEqual(R.verify_route(copy.deepcopy(leg), expected)["cwd"], str(expected))
        self.assertEqual(self.git(expected, "branch", "--show-current"), "framed-fixture")
        self.assertEqual(self.git(self.primary, "worktree", "list", "--porcelain"), worktrees)
        self.assertEqual(self.primary_fingerprint(), self.primary_before)

    def test_only_the_frames_own_live_allocation_is_inherited(self):
        frame, cwd = self.route, self.route["cwd"]
        self.assertTrue(R._inherits_frame_worktree(frame, CODE_STAGED, 0, cwd))
        for changed in ({"reason": "approved-linked-task"}, {"state": "retained-linked"}, {"state": "failed"}):
            other = {**frame, "framed_worktree": {**frame["framed_worktree"], **changed}}
            self.assertFalse(R._inherits_frame_worktree(other, CODE_STAGED, 0, cwd), changed)
        self.assertFalse(R._inherits_frame_worktree(frame, {**CODE_STAGED, "cwd": cwd}, 0, cwd))
        self.assertFalse(R._inherits_frame_worktree(frame, CODE_STAGED, 1, cwd))
        self.assertFalse(R._inherits_frame_worktree(frame, CODE_STAGED, 0, str(self.primary)))
        self.assertFalse(R._inherits_frame_worktree({**frame, "framed_worktree": {}}, CODE_STAGED, 0, cwd))

    def test_an_allocation_moved_to_another_branch_is_not_taken_and_the_leg_prepares_as_before(self):
        self.git(self.route["cwd"], "switch", "-q", "-c", "elsewhere")
        result = self.settle()
        self.assertTrue(result["owner_started"], result)
        self.assertEqual(R.prepare_isolated_worktree.call_count, 1)
        leg_path, = self.leg_routes()
        leg = json.loads(leg_path.read_text(encoding="utf-8"))
        self.assertEqual(leg["cwd"], str((self.primary.parent / "repo-wt" / "framed-fixture-2").resolve()))
        self.assertNotIn("framed_worktree", leg)
        self.assertEqual(self.git(self.route["cwd"], "branch", "--show-current"), "elsewhere")


class OneLegFramedTest(StartBase):
    """Explicit simple quick requests keep one deep leg and follow the same decision."""
    FRAME_INTENSITY = "quick"

    def test_one_leg_reads_one_brief_and_its_proposal_starts_the_first_leg(self):
        self.assertEqual(RP.frame_legs(self.route), ("frame",))
        root, record, output = W._framed_cycle(self.route)
        rows = W._proposal_rows(self.route, self.jobs, root, record, output)
        self.assertEqual([row["node"] for row in rows], ["frame"])
        route_question, = W._interview_questions(rows)
        self.assertEqual((route_question["kind"], [o.get(FI.PROPOSAL_MARK) for o in route_question["options"]]),
                         ("yes-no", ["frame", None]))
        result = self.settle()
        self.assertEqual(result["state"], "inline", result)
        decision = self.record()["decision"]
        self.assertEqual([row["node"] for row in decision["briefs"]], ["frame"])
        self.assertEqual([row["node"] for row in decision["proposals"]], ["frame"])
        self.assertEqual(len(self.leg_routes()), 1)

    def test_a_none_ending_names_only_the_one_leg(self):
        del self.interview["route_proposals"]
        NoneEndingTest.assert_none(self, "proposal-not-read")
        self.assertEqual([row["node"] for row in self.record()["decision"]["proposals"]], ["frame"])


class LegacyFramedStartTest(StartBase):
    """Starting a basis-free historical route uses its sealed frame node count."""

    run_fake = F.FramedStartTest.run_fake

    def start(self, **kw):
        return W.start_work(self.route, self.path, self.jobs, run=self.run_fake, **kw)

    def start_legacy(self, intensity, expected_nodes):
        route = F.legacy_framed_route(self, intensity)
        self.route = R.verify_route(route, R.ROOT)
        self.path = Path(L.admit_runtime_route(self.root, self.route).route_file)
        self.calls = []
        with mock.patch.object(W, "join_selected_attempts", return_value={"state": "timeout", "children": []}):
            result = self.start()
        self.assertEqual(result["state"], "preparing", result)
        launched = [command[command.index("--route-node") + 1] for command in self.calls]
        self.assertEqual(launched, expected_nodes)
        self.assertEqual(len(self.calls), len(expected_nodes))

    def test_basis_free_historical_one_leg_route_starts_only_frame(self):
        self.start_legacy("quick", ["frame"])

    def test_basis_free_historical_two_leg_route_starts_both_frames(self):
        self.start_legacy("strong", F.FRAME_IDS)


class LegPlanTest(StartBase):
    """The plan fields the frame wrote reach the owner and every stage worker, sealed, never copied."""

    PLANNED = {**CODE_STAGED, "done_when": ["the abort tests pass", {"text": "latency report written",
                                                                     "check": "test -f reports/latency.md"}],
               "verify": "run the abort tests and compare the median", "hands_over": ["reports/latency.md"]}

    def planned_leg(self):
        self.set_briefs(self.PLANNED, self.PLANNED)
        self.set_interview({"legs": [self.PLANNED]})
        self.settle()
        (path,) = self.leg_routes()
        return json.loads(path.read_text(encoding="utf-8"))

    def test_the_leg_plan_and_the_adopted_brief_are_read_from_the_sealed_decision(self):
        route = self.planned_leg()
        plan = RP.leg_plan(route)
        self.assertEqual([item["id"] for item in plan["leg"]["done_when"]], ["d1", "d2"])
        self.assertEqual(plan["leg"]["verify"], "run the abort tests and compare the median")
        self.assertTrue(plan["adopted_brief"].endswith("shards/frame/direction-brief.md"))
        task = self.leg_calls[0][self.leg_calls[0].index("--prompt-text") + 1]
        self.assertIn("frame/direction-brief.md (sha256", task)
        self.assertIn("adopted direction: the person chose this brief's route", task)
        self.assertIsNone(RP.leg_plan({**route, "route_plan": None}))

    def test_sixteen_parallel_slices_survive_selection_and_first_leg_card_without_graph_changes(self):
        parallel = [f"slice {index:02d}" for index in range(16)]
        planned = {**CODE_STAGED, "parallel": parallel}
        self.set_briefs(planned, planned)
        self.set_interview({"legs": [planned]})
        notice = io.StringIO()
        with contextlib.redirect_stderr(notice):
            self.settle()

        record = self.record()
        decision = record["decision"]
        self.assertEqual(decision["proposal"]["legs"][0]["parallel"], parallel)
        self.assertFalse(any("ignored:legs[0].parallel" in note
                             for row in decision["proposals"] for note in row.get("read_notes", [])))
        route = json.loads(self.leg_routes()[0].read_text(encoding="utf-8"))
        self.assertEqual(RP.leg_plan(route)["leg"]["parallel"], parallel)
        self.assertEqual([node["id"] for node in route["nodes"]], ["plan", "execute", "test", "report"])
        card_lines = [line.strip() for line in notice.getvalue().splitlines()
                      if line.strip().startswith("승인된 분할:")]
        self.assertEqual(len(card_lines), 1)
        self.assertIn("slice 00 · slice 01 · slice 02", card_lines[0])
        self.assertIn("외 13개(route·owner 지시문에 전부)", card_lines[0])
        self.assertNotIn("slice 03", card_lines[0])

        planned_graph = ([ (node["id"], node.get("depends_on")) for node in route["nodes"] ],
                         route.get("human_gates"), route.get("parallel_groups"))
        self.tearDown()
        self.setUp()
        self.set_briefs(CODE_STAGED, CODE_STAGED)
        self.set_interview({"legs": [CODE_STAGED]})
        self.settle()
        baseline = json.loads(self.leg_routes()[0].read_text(encoding="utf-8"))
        baseline_graph = ([(node["id"], node.get("depends_on")) for node in baseline["nodes"]],
                          baseline.get("human_gates"), baseline.get("parallel_groups"))
        self.assertEqual(planned_graph, baseline_graph)

    def test_owner_and_verification_prompts_carry_the_plan_and_the_items_file(self):
        import worker_bootstrap as WB
        route = self.planned_leg()
        owner = WB.plan_leg_prompt(route, None)
        self.assertIn("- d1: the abort tests pass", owner)
        self.assertIn("- d2: latency report written (check: `test -f reports/latency.md`)", owner)
        self.assertIn("Verify: run the abort tests and compare the median", owner)
        self.assertNotIn("items.json", owner)
        test_node = next(n["id"] for n in route["nodes"] if str(n.get("unit") or "").startswith("qa/"))
        primary = str(self.root / "test-report.md")
        verifier = WB.plan_leg_prompt(route, test_node, primary)
        self.assertIn(f"`{primary}.items.json`", verifier)
        self.assertIn('"route_id": "' + route["route_id"] + '"', verifier)
        self.assertEqual(WB.plan_leg_prompt({**route, "route_plan": None}, test_node), "")

    def test_the_items_file_is_read_against_the_sealed_ids(self):
        route = self.planned_leg()
        artifact = self.root / "test-report.md"
        artifact.write_text("report\n", encoding="utf-8")
        sidecar = RP.leg_items_path(artifact)
        write = lambda items, **extra: sidecar.write_text(json.dumps(
            {"schema": "leg_items_v1", "route_id": route["route_id"], "node": "test", "attempt_id": "att-1",
             "items": items, **extra}), encoding="utf-8")
        self.assertIsNone(RP.read_leg_items(route, artifact))                      # no file: cannot judge
        write([{"id": "d1", "state": "met", "note": "ok"}, {"id": "d2", "state": "unknown"}])
        self.assertEqual(RP.read_leg_items(route, artifact),
                         {"ids": ["d1", "d2"], "unmet": ["d2"], "node": "test", "attempt_id": "att-1"})
        write([{"id": "d1", "state": "met"}])                                      # a left-out id is unmet
        self.assertEqual(RP.read_leg_items(route, artifact)["unmet"], ["d2"])
        for items, extra in (([{"id": "d3", "state": "met"}], {}), ([{"id": "d1", "state": "done"}], {}),
                             ([{"id": "d1", "state": "met"}, {"id": "d1", "state": "met"}], {}),
                             ([{"id": "d1", "state": "met"}], {"route_id": "rt-other"})):
            with self.subTest(items=items, extra=extra):
                write(items, **extra)
                self.assertIsNone(RP.read_leg_items(route, artifact))


class BlockedProgressTest(LegPlanTest):
    """RA-5: BLOCKED rounds that shrink the leg's unmet items are progress, not a stalled node."""

    def blocked_round(self, route, number, unmet, *, attempt=None, node="test", items=True):
        attempt = attempt or f"att-test-{number}"
        output = self.root / "rounds" / f"r{number}"
        output.mkdir(parents=True, exist_ok=True)
        artifact = output / "test-report.md"
        artifact.write_text("report\n", encoding="utf-8")
        if items:
            RP.leg_items_path(artifact).write_text(json.dumps({
                "schema": "leg_items_v1", "route_id": route["route_id"], "node": node, "attempt_id": attempt,
                "items": [{"id": i, "state": "unmet" if i in unmet else "met"} for i in ("d1", "d2")]}),
                encoding="utf-8")
        log = output / "worker.log"
        events = [{"type": "item.completed", "item": {"type": "agent_message",
                   "text": f"artifact: {artifact}\nverdict: BLOCKED\nblocker: items left"}},
                  {"type": "turn.completed"}]
        log.write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")
        return ("done", {"attempt_id": f"att-test-{number}", "note": "dead-worker-blocked", "failure_class": "blocked",
                         "worker_type": "stage", "route_node": "test", "log_file": str(log),
                         "artifact_root": str(self.root)})

    def budget(self, route, rows, **node_fields):
        import review_round_cap as RC
        node = {**next(n for n in route["nodes"] if n["id"] == "test"), **node_fields}
        # The worker's artifact root, as the runtime that launched it names it.
        with mock.patch.dict(os.environ, {"AGENT_ARTIFACT_ROOT": str(self.root)}):
            return RC.round_budget(route, node, rows)

    def test_shrinking_unmet_items_keep_the_node_going_and_a_stall_still_binds_it(self):
        route = self.planned_leg()
        shrinking = [self.blocked_round(route, 1, {"d1", "d2"}), self.blocked_round(route, 2, {"d2"})]
        budget = self.budget(route, shrinking)
        self.assertEqual((budget.state, budget.verdictless_streak, budget.progress_reset), ("admit", 1, True))
        stalled = shrinking + [self.blocked_round(route, 3, {"d2"})]
        self.assertEqual(self.budget(route, stalled).state, "verdictless-bound")
        grown = [self.blocked_round(route, 1, {"d2"}), self.blocked_round(route, 2, {"d1", "d2"})]
        self.assertEqual(self.budget(route, grown).state, "verdictless-bound")

    def test_items_that_cannot_be_judged_keep_the_two_in_a_row_rule(self):
        route = self.planned_leg()
        for label, second in (("no file", self.blocked_round(route, 2, set(), items=False)),
                              ("another attempt", self.blocked_round(route, 2, set(), attempt="att-other")),
                              ("another node", self.blocked_round(route, 2, set(), node="review"))):
            with self.subTest(label):
                rows = [self.blocked_round(route, 1, {"d1", "d2"}), second]
                self.assertEqual(self.budget(route, rows).state, "verdictless-bound")
        # A row that names no worker type reads as its node's: a review's FAIL is no verdict.
        bare = lambda row: (row[0], {k: v for k, v in row[1].items() if k != "worker_type"})
        failed = ("done", {"attempt_id": "att-test-9", "note": "dead-worker-fail", "failure_class": "fail",
                           "route_node": "test"})
        rows = [bare(self.blocked_round(route, 1, {"d1", "d2"})), failed, bare(self.blocked_round(route, 2, {"d2"}))]
        self.assertEqual(self.budget(route, rows, kind="review-worker").state, "verdictless-bound")
        planless = {**route, "route_plan": None}
        rows = [self.blocked_round(route, 1, {"d1", "d2"}), self.blocked_round(route, 2, {"d2"})]
        self.assertEqual(self.budget(planless, rows).state, "verdictless-bound")


class PlanExtraStageTest(StartBase):
    """A plan's extra stage is a real node shaped on a catalog stage, with that stage's gate and budget."""

    EXTRA = {**CODE_STAGED, "extra_stages": [{"id": "measure", "unit": "qa/test", "after": "test",
                                              "verify": "run the latency script three times"}]}

    def leg_route(self, leg):
        self.set_briefs(leg, leg)
        self.set_interview({"legs": [leg]})
        result = self.settle()
        paths = self.leg_routes()
        self.assertEqual(len(paths), 1, json.dumps(self.record()["decision"].get("proposals"), default=str)[:3000])
        (path,) = paths
        return json.loads(path.read_text(encoding="utf-8"))

    def test_the_extra_stage_runs_after_its_stage_with_the_templates_gate_budget_and_own_files(self):
        import review_round_cap as RC
        route = self.leg_route(self.EXTRA)
        ids = [n["id"] for n in route["nodes"]]
        self.assertEqual(ids[ids.index("test") + 1], "plan-measure")
        nodes = {n["id"]: n for n in route["nodes"]}
        measure, test = nodes["plan-measure"], nodes["test"]
        self.assertEqual((measure["unit"], measure["kind"], measure["completion_gate"]),
                         (test["unit"], test["kind"], test["completion_gate"]))
        self.assertEqual(measure["plan_stage"], {"id": "measure", "template": "test",
                                                 "verify": "run the latency script three times"})
        self.assertEqual(measure["depends_on"], ["test"])
        self.assertIn("plan-measure", nodes["report"]["depends_on"])
        self.assertTrue(all("parts/plan/measure/" in out for out in measure["outputs"]), measure["outputs"])
        self.assertTrue(all("parts/plan/measure" in scope for scope in measure["write_scope"]))
        self.assertTrue(RC.is_round_capped_node(measure))                  # same budget as the stage it copies
        self.assertEqual(R.verify_route(json.loads(json.dumps(route)))["route_id"], route["route_id"])
        import worker_bootstrap as WB
        self.assertIn("run the latency script three times", WB.plan_leg_prompt(route, "plan-measure"))

    def test_an_extra_stage_that_cannot_be_shaped_or_placed_is_left_out_and_named(self):
        leg = {**CODE_STAGED, "extra_stages": [{"id": "x", "unit": "no/such-unit", "after": "test"},
                                               {"id": "y", "unit": "qa/test", "after": "report"},
                                               {"id": "z", "unit": "qa/test", "after": "nowhere"},
                                               # an approval-gated stage (deploy, full-run...) is never a template
                                               {"id": "w", "unit": "_kernel/resource", "after": "test"},
                                               # a stage that changes the source would land after the last check
                                               {"id": "patch", "unit": "dev/backend", "after": "test"}]}
        route = self.leg_route(leg)
        self.assertFalse([n for n in route["nodes"] if n.get("plan_stage")])
        row = next(r for r in self.record()["decision"]["proposals"] if r["node"] == "frame")
        self.assertEqual(sorted(note for note in row["read_notes"] if note.startswith("extra-stage")),
                         ["extra-stage-not-compiled:patch", "extra-stage-not-compiled:w", "extra-stage-not-compiled:x",
                          "extra-stage-not-compiled:y", "extra-stage-not-compiled:z"])


class FrameFrictionContinuationTest(StartBase):
    def readiness(self, frame_route, jobs):
        return {"tuples": [{**T.nested(parent, child), "checked_worktree": frame_route["cwd"]}
                           for parent in ("claude", "codex", "opencode")
                           for child in ("claude", "codex", "opencode")],
                "candidates": T.registered_headless()["candidates"]}

    def new_repository_frame(self, *, existing=False, contents=False):
        self.parent = self.root.parent / "new-project-parent"
        self.parent.mkdir()
        self.destination = self.parent / "new-project"
        if existing:
            self.destination.mkdir()
            if contents:
                (self.destination / "keep.txt").write_text("user data")
        task = f"Create a new local git repository at {self.destination}\nImplement the requested tool."
        old_path = self.path
        self.route, self.path = self.admitted(cwd=str(self.parent),
            dispatch_evidence={"tuples": [{**T.nested("claude", "codex"), "checked_worktree": str(self.parent)}]},
            work_request={"text": task, "owner_harness": None})
        old_path.unlink()  # replace only this test fixture's initial dummy frame
        self.output = self.begin_cycle(self.path)
        self.write_frame_outputs(self.output)
        self.leg = {"capability": "autopilot-code", "shape": "solo", "cwd": str(self.destination)}
        self.set_briefs(self.leg, self.leg)
        self.set_interview({"legs": [self.leg]})

    def test_approved_absent_repository_starts_in_its_destination_and_records_creator(self):
        self.new_repository_frame()
        rows = W._proposal_rows(self.route, self.jobs, *W._framed_cycle(self.route))
        self.assertEqual(rows[0]["repository_preparation"][0]["state"], "create-after-approval")
        self.assertLessEqual(len(W._repository_notice(rows)), 2)
        self.assertIsNone(W._automatic_direction(self.route, rows, self.output))
        self.assertFalse(self.destination.exists())
        receipt = self.settle()
        self.assertTrue(receipt["owner_started"], receipt)
        record = self.record()
        created = record["first_leg"]["repository_created"]
        self.assertEqual(created["path"], str(self.destination))
        self.assertTrue(created["at"])
        self.assertTrue((self.destination / ".git").is_dir())
        leg = json.loads(self.leg_routes()[0].read_text())
        self.assertEqual(leg["cwd"], str(self.destination))
        self.assertIn("Hearting created " + str(self.destination), leg["work_request"]["text"])
        self.assertIn(created["at"], leg["work_request"]["text"])
        head = subprocess.check_output(["git", "-C", str(self.destination), "rev-parse", "HEAD"], text=True).strip()
        self.assertEqual(head, record["first_leg"]["repository_baseline"]["commit"])
        self.assertEqual(len(self.leg_routes()), 1)
        self.settle()
        self.assertEqual(len(self.leg_routes()), 1)

    def test_existing_empty_or_populated_folder_is_untouched_and_uses_existing_choices(self):
        self.new_repository_frame(existing=True, contents=True)
        rows = W._proposal_rows(self.route, self.jobs, *W._framed_cycle(self.route))
        self.assertEqual(rows[0]["repository_preparation"][0]["state"], "manual")
        self.assertEqual(len(W._interview_questions(rows)), 1)
        receipt = self.settle()
        self.assertEqual(receipt["reason"], "repository-preparation-pending")
        self.assertEqual(sorted(p.name for p in self.destination.iterdir()), ["keep.txt"])
        self.assertEqual((self.destination / "keep.txt").read_text(), "user data")
        (self.destination / "keep.txt").unlink()
        self.settle()
        self.assertEqual(list(self.destination.iterdir()), [])

    def test_explicit_new_repository_destination_fills_an_omitted_first_leg_cwd(self):
        self.new_repository_frame()
        self.leg.pop("cwd")
        self.set_briefs(self.leg, self.leg)
        rows = W._proposal_rows(self.route, self.jobs, *W._framed_cycle(self.route))
        self.assertEqual(rows[0]["proposal"]["legs"][0]["cwd"], str(self.destination))
        self.set_interview(rows[0]["proposal"])
        self.assertTrue(self.settle()["owner_started"])
        leg = json.loads(self.leg_routes()[0].read_text())
        self.assertEqual(leg["cwd"], str(self.destination))

    def test_interrupted_baseline_binding_resumes_the_same_repository_without_reinitializing(self):
        self.new_repository_frame()
        real_run = subprocess.run
        def fail_ref(argv, **kwargs):
            if argv[0] == "git" and "update-ref" in argv:
                return subprocess.CompletedProcess(argv, 1, "", "temporary ref failure")
            return real_run(argv, **kwargs)
        with mock.patch.object(subprocess, "run", side_effect=fail_ref):
            self.assertEqual(self.settle()["reason"], "repository-preparation-pending")
        created = dict(self.record()["first_leg"]["repository_created"])
        baseline = dict(self.record()["first_leg"]["repository_baseline"])
        self.assertTrue(self.settle()["owner_started"])
        self.assertEqual(self.record()["first_leg"]["repository_created"], created)
        self.assertEqual(self.record()["first_leg"]["repository_baseline"], baseline)

    def test_public_correct_retains_frame_input_and_redirects_after_owner_start(self):
        import dispatch_owner_input as inputs
        condition = self.root.parent / "condition.txt"
        condition.write_text("Use local sources only")
        out = io.StringIO()
        argv = ["capability-route.py", "correct", "--attempt-id", str(self.path), "--jobs", str(self.jobs),
                "--message-file", str(condition)]
        with mock.patch.object(sys, "argv", argv), contextlib.redirect_stdout(out):
            self.assertEqual(R.main(), 0)
        self.assertEqual(json.loads(out.getvalue())["state"], "frame-stored")
        self.set_briefs({"capability": "autopilot-code", "shape": "solo"},
                        {"capability": "autopilot-code", "shape": "solo"})
        self.set_interview({"legs": [{"capability": "autopilot-code", "shape": "solo"}]})
        receipt = self.settle()
        redirected = inputs.frame_input(self.route, "later condition", jobs=self.jobs)
        self.assertEqual(redirected, {"redirect_owner": receipt["owner_attempt_id"]})

    def test_new_repository_in_a_quoted_parser_example_is_not_creation_authority(self):
        route = {"cwd": "/workspace", "work_request": {"text":
            "Fix parsing of the phrase new git repository /tmp/example.\n"
            "```text\nCreate a new git repository /tmp/quoted\n```"}}
        self.assertIsNone(RP.requested_repository(route))

    def test_replaced_git_directory_is_preserved_on_preparation_retry(self):
        self.new_repository_frame()
        real_run = subprocess.run
        def fail_ref(argv, **kwargs):
            if argv[0] == "git" and "update-ref" in argv:
                return subprocess.CompletedProcess(argv, 1, "", "temporary ref failure")
            return real_run(argv, **kwargs)
        with mock.patch.object(subprocess, "run", side_effect=fail_ref):
            self.assertEqual(self.settle()["reason"], "repository-preparation-pending")
        (self.destination / ".git").rename(self.parent / "created-git")
        foreign = self.parent / "foreign"
        subprocess.run(["git", "init", "--quiet", str(foreign)], check=True)
        before = (foreign / ".git/config").read_bytes()
        (self.destination / ".git").symlink_to(foreign / ".git", target_is_directory=True)
        receipt = self.settle()
        self.assertEqual(receipt["reason"], "repository-preparation-pending")
        self.assertIn("Git directory was replaced", receipt["detail"])
        self.assertEqual((foreign / ".git/config").read_bytes(), before)
        self.assertFalse((foreign / ".git/refs/heads/master").exists())

    def test_frame_conditions_are_deduplicated_and_reach_interview_owner_and_existing_fleet_detail(self):
        import dispatch_owner_input as inputs
        import work_intent
        from types import SimpleNamespace
        from fleet import details
        basic = SimpleNamespace(sessions=[], jobs=[], resources=[], node_evidence={self.route["route_id"]: {
            "frame": {"route_file": str(self.path)}}})
        before = details.state_key(basic)
        original = self.path.read_bytes()
        first = inputs.frame_input(self.route, "Use local sources only", "local-only")
        again = inputs.frame_input(self.route, "Use local sources only", "local-only")
        self.assertEqual((first["retained_corrections"], again["duplicate"]), (1, True))
        self.assertNotEqual(details.state_key(basic), before)
        template = W.frame_interview_step(self.route, self.path, self.jobs)
        self.assertIn("Use local sources only", template["interview_template"]["brief"]["constraints"])
        self.settle()
        leg = json.loads(self.leg_routes()[0].read_text())
        self.assertIn("Use local sources only", leg["work_request"]["text"])
        self.assertIn("Use local sources only", work_intent.prompt_block(leg))
        from fleet import route as fleet_route
        from fleet.explorer.live import _node_detail
        view = fleet_route.with_frame_corrections({"nodes": [{"id": "frame", "state": "failed",
                                                  "note": "dead-worker-blocked"}]}, self.route)
        self.assertIn("correct 1건", view["nodes"][0]["says"])
        self.assertEqual(view["nodes"][0]["note"], "dead-worker-blocked")
        self.assertIn("Use local sources only", _node_detail(view["nodes"][0], 0)["says"])
        self.assertEqual(self.path.read_bytes(), original)

    def test_explicit_output_conflicts_warn_without_rejecting_or_confusing_relative_paths(self):
        question = {"brief": {"outcome": "Write output/report.md"}, "questions": [],
            "route_proposals": {"by_option": {"go": {"legs": [{"done_when": [
                {"text": "Report exists", "check": "test -f reports/report.md"}]}]}}}}
        warnings = FI.path_conflicts(question, "/workspace")
        self.assertEqual(len(warnings), 1)
        self.assertEqual(warnings[0]["done_when_path"], "/workspace/reports/report.md")
        question["brief"]["outcome"] = "Write ./reports/report.md"
        self.assertEqual(FI.path_conflicts(question, "/workspace"), [])
        question["brief"]["outcome"] = "Write another file"
        self.assertEqual(FI.path_conflicts(question, "/workspace"), [])
        question["brief"]["outcome"] = "Copy old/report.md to reports/report.md"
        self.assertEqual(FI.path_conflicts(question, "/workspace"), [])


class FailureTableTest(StartBase):
    def test_manually_composed_first_leg_gets_guarded_frame_closure_commands_without_mutation(self):
        self.settle()
        leg_path = self.leg_routes()[0]
        leg = json.loads(leg_path.read_text())
        record = self.record()
        record.pop("first_leg")
        self.record_path().write_bytes(RP.render(record))
        R.outcome_path(self.path).unlink()
        before = self.record_path().read_bytes()
        guidance = W._manual_frame_closure(leg, self.jobs)
        receipt = W.start_work(leg, leg_path, self.jobs, run=self.fake_run)
        self.assertEqual(receipt["frame_closure"], guidance)
        self.assertEqual(guidance["frame_route_id"], self.route["route_id"])
        self.assertEqual(self.record_path().read_bytes(), before)
        self.assertFalse(R.outcome_path(self.path).exists())
        self.assertEqual(len(guidance["commands"]), 3)
        for command in guidance["commands"][:2]:
            argv = shlex.split(command)
            self.in_process_cli(self.jobs, *argv[2:])
        self.assertTrue(self.outcome()["terminal_gate_proven"])
        self.assertEqual(self.record_path().read_bytes(), before)
        self.assertEqual(len(self.leg_routes()), 1)
        self.assertIsNone(W._manual_frame_closure(leg, self.jobs))

    def test_a164_11_a_first_leg_compose_refusal_keeps_the_record_and_the_same_start_resumes(self):
        with mock.patch.object(R, "compile_first_leg", side_effect=ValueError("compose-graph-order:test-before-execute")):
            blocked = self.settle()
        self.assertEqual((blocked["state"], blocked["reason"]), ("needs-attention", "first-leg-compose-refused"), blocked)
        self.assertIn("compose-graph-order", blocked["detail"])
        self.assertNotIn("first_leg", self.record())
        self.assertEqual(self.record()["decision"]["selected"], ROUTE_LABELS[0])
        self.assertFalse(R.outcome_path(self.path).exists())          # the frame route stays open
        self.assertEqual((self.leg_routes(), self.registry_rows(), self.cli_calls), ([], [], []))
        resumed = self.settle()
        self.assertEqual(resumed["state"], "inline", resumed)
        self.assertEqual(len(self.leg_routes()), 1)
        self.assertEqual(self.cli_calls, ["complete", "close"])

    def test_a164_11_a_first_leg_that_waits_for_capacity_returns_its_own_receipt_and_the_frame_stays_open(self):
        self.set_briefs(CODE_STAGED, CODE_STAGED)
        self.set_interview({"legs": [CODE_STAGED]})

        def refused(command, **kwargs):
            self.leg_calls.append(command)
            return subprocess.CompletedProcess(command, 75, "check=failed\nreason=model-worker-governor-denied\n"
                                               "child_spawned=0\nretryable=1\nrefusal=class-cap\n"
                                               "worker_class=owner\nretry_after_seconds=30\n", "")
        waiting = self.settle(run=refused)
        self.assertEqual((waiting["state"], waiting["reason"]), ("waiting-capacity", "launch-capacity-wait"), waiting)
        self.assertEqual(waiting["required_action"], "resume-after-capacity")
        self.assertEqual(waiting["route_id"], self.record()["first_leg"]["route"]["route_id"])   # the leg's own receipt
        self.assertNotIn("start_receipt", self.record()["first_leg"])
        self.assertFalse(R.outcome_path(self.path).exists())
        self.assertEqual((self.cli_calls, self.registry_rows()), ([], []))
        done = self.settle()
        self.assertEqual(done["state"], "running", done)
        self.assertEqual(len(self.leg_routes()), 1)
        self.assertEqual(len(self.registry_rows()), 1)
        self.assertEqual(self.cli_calls, ["complete", "close"])

    def test_a_launch_that_is_not_admitted_is_the_first_legs_own_needs_attention(self):
        self.set_briefs(CODE_STAGED, CODE_STAGED)
        self.set_interview({"legs": [CODE_STAGED]})
        notice = io.StringIO()
        with contextlib.redirect_stderr(notice):
            result = self.settle(run=lambda command, **kw: subprocess.CompletedProcess(command, 65, "check=failed\nreason=x\n", ""))
        self.assertNotIn("[경로]", notice.getvalue())
        self.assertEqual(result["state"], "needs-attention", result)
        self.assertEqual(result["reason"], "owner-launch-not-admitted")
        self.assertFalse(R.outcome_path(self.path).exists())

    def test_two_starts_at_once_fix_one_decision_one_route_and_one_attempt(self):
        import threading
        self.set_briefs(CODE_STAGED, CODE_STAGED)
        self.set_interview({"legs": [CODE_STAGED]})
        results, errors = [], []

        def run():
            try:
                results.append(self.settle())
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
        threads = [threading.Thread(target=run) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 2)
        self.assertEqual((len(self.leg_routes()), len(self.registry_rows()), len(self.leg_calls)), (1, 1, 1))
        self.assertEqual(self.cli_calls.count("complete"), 1)


class FirstLegStateReplayTest(StartBase):
    """Re-reading the frame route answers with the first leg's state now, never its old launch receipt (defect 4)."""

    SECOND = {"capability": "autopilot-code", "shape": "direct", "why": "then the second"}

    def replay(self):
        from dispatch_notice_state import closed_outcome
        return self.settle(closed=closed_outcome(Path(self.path), self.route))

    def leg(self):
        (leg_path,) = self.leg_routes()
        return json.loads(leg_path.read_text(encoding="utf-8")), leg_path

    def staged_leg(self, legs=(CODE_STAGED,)):
        self.set_briefs(list(legs), list(legs))
        self.set_interview({"legs": list(legs)})
        return self.settle()

    def end_owner_row(self):
        """The owner's registry row, as a finished attempt's row reads (schema 2, status done)."""
        row = self.jobs.read_text(encoding="utf-8").replace("\topen\t", "\tdone\t", 1)
        self.jobs.write_text(row.rstrip("\n") + ",attempt_schema_version=2\n", encoding="utf-8")

    def untouched(self, before):
        self.assertEqual((len(self.leg_routes()), len(self.registry_rows()), len(self.leg_calls), self.cli_calls),
                         before)
        self.assertEqual(self.record_path().read_bytes(), self.record_bytes)

    def snapshot(self):
        self.record_bytes = self.record_path().read_bytes()
        return (len(self.leg_routes()), len(self.registry_rows()), len(self.leg_calls), list(self.cli_calls))

    def test_a_finished_direct_leg_answers_completed_not_its_old_execute_inline_receipt(self):
        first = self.settle()
        self.assertEqual((first["state"], first["required_action"]), ("inline", "execute-inline"))
        route, leg_path = self.leg()
        self.finish_leg(route, leg_path)
        before = self.snapshot()
        for _ in range(2):
            again = self.replay()
            self.assertEqual((again["state"], again["required_action"]), ("completed", "advance-completed"), again)
            self.assertEqual(again["route_id"], route["route_id"])
            self.assertEqual(again["route_decision"]["frame_route_id"], self.route["route_id"])
            self.assertNotIn("task", again)
            self.assertNotIn("artifact_env", again)
            self.assertNotIn("next_leg", again)               # the only leg of its plan
        self.untouched(before)

    def started_leg(self, result):
        path = Path(result["plan_advanced"]["route_file"])
        return json.loads(path.read_text(encoding="utf-8")), path

    def cursor(self):
        path = RP.plan_cursor_path(self.root, self.route["route_id"])
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []

    def test_a_finished_leg_starts_the_plans_next_leg_once(self):
        """Plan cursor: the person approved both legs, so the next one starts without another question."""
        self.set_briefs([DIRECT, self.SECOND], [DIRECT, self.SECOND])
        self.set_interview({"legs": [DIRECT, self.SECOND]})
        self.settle()
        route, leg_path = self.leg()
        cycle = self.finish_leg(route, leg_path)
        again = self.replay()
        self.assertEqual((again["state"], again["required_action"]), ("inline", "execute-inline"), again)
        self.assertEqual(again["plan_advanced"]["leg"], 1)
        second, second_path = self.started_leg(again)
        self.assertEqual((again["route_id"], second["route_plan"]["index"]), (second["route_id"], 1))
        self.assertEqual(P.read_cycle_record(self.root, again["artifact_env"]["AGENT_ARTIFACT_CYCLE_ID"])
                         ["parent_cycle_id"], cycle)
        rows = self.cursor()
        self.assertEqual([(row["index"], row["route_id"], row["after_route_id"]) for row in rows],
                         [(1, second["route_id"], route["route_id"])])
        self.assertEqual(rows[0]["by"], {"session": "parent"})
        # The same resume line again: the frame answers for the leg already started, compiles nothing.
        before = len(self.leg_routes())
        replayed = self.replay()
        self.assertEqual((replayed["route_id"], replayed["state"]), (second["route_id"], "inline"), replayed)
        self.assertEqual((len(self.leg_routes()), len(self.cursor())), (before, 1))

    def test_the_next_legs_own_start_arms_its_resume_and_the_finished_leg_arms_none(self):
        self.set_briefs([DIRECT, self.SECOND], [DIRECT, self.SECOND])
        self.set_interview({"legs": [DIRECT, self.SECOND]})
        self.settle()
        route, leg_path = self.leg()
        self.finish_leg(route, leg_path)
        armed = []
        real = W._arm_capacity_resume
        with mock.patch.object(W, "_arm_capacity_resume",
                               side_effect=lambda result, path, jobs: armed.append(str(path)) or real(result, path, jobs)):
            again = W.start_work(route, leg_path, self.jobs)
        self.assertEqual(armed, [again["plan_advanced"]["route_file"]])

    def test_a_successor_session_continues_the_plan_on_the_same_resume_line(self):
        self.set_briefs([DIRECT, self.SECOND], [DIRECT, self.SECOND])
        self.set_interview({"legs": [DIRECT, self.SECOND]})
        self.settle()
        route, leg_path = self.leg()
        self.finish_leg(route, leg_path)
        with mock.patch.object(W, "default_parent_session_id", return_value="successor"):
            again = self.replay()
        self.assertEqual((again["state"], again["plan_advanced"]["leg"]), ("inline", 1), again)
        self.assertEqual(self.cursor()[0]["by"], {"session": "successor"})

    def test_a_three_leg_plan_runs_to_its_end_on_the_same_resume_line(self):
        third = {**self.SECOND, "why": "then the third"}
        self.set_briefs([DIRECT, self.SECOND, third], [DIRECT, self.SECOND, third])
        self.set_interview({"legs": [DIRECT, self.SECOND, third]})
        self.settle()
        current, path = self.leg()
        for expected in (1, 2):
            self.finish_leg(current, path)
            again = self.replay()
            self.assertEqual((again["state"], again["plan_advanced"]["leg"]), ("inline", expected), again)
            current, path = self.started_leg(again)
        self.finish_leg(current, path)
        before = self.snapshot()
        last = self.replay()
        self.assertEqual((last["state"], last["required_action"]), ("completed", "advance-completed"), last)
        self.assertEqual((last["route_id"], current["route_plan"]["index"]), (current["route_id"], 2))
        self.assertEqual(last["route_decision"]["frame_route_id"], self.route["route_id"])
        self.assertNotIn("next_leg", last)
        self.assertNotIn("plan_advanced", last)
        self.assertEqual([row["index"] for row in self.cursor()], [1, 2])
        self.untouched(before)

    def test_a_plan_approved_for_a_report_stops_after_its_leg_with_the_next_leg_as_information(self):
        self.set_briefs([DIRECT, self.SECOND], [DIRECT, self.SECOND])
        self.set_interview({"legs": [DIRECT, self.SECOND], "execution_scope": "report"})
        self.settle()
        route, leg_path = self.leg()
        self.finish_leg(route, leg_path)
        before = self.snapshot()
        again = self.replay()
        self.assertEqual((again["state"], again["next_leg"]["index"]), ("completed", 1), again)
        self.assertNotIn("plan_advanced", again)
        self.assertEqual(self.cursor(), [])
        self.untouched(before)

    def test_a_leg_composed_again_keeps_its_stages_intensity_pins_and_plan(self):
        self.staged_leg()
        route, _ = self.leg()
        argv = shlex.split(W._compose_again(route))
        self.assertEqual(argv[argv.index("--graph") + 1], "plan,execute,test,report")
        self.assertEqual(argv[argv.index("--intensity") + 1], route["effective_intensity"])
        self.assertTrue(argv[argv.index("--route-plan") + 1].endswith("route-decision.json#0"))
        for token in RP.pin_tokens(route.get("selection_pins")):
            self.assertIn(token, argv)

    def test_a_finished_staged_owner_leg_answers_completed(self):
        first = self.staged_leg()
        self.assertEqual(first["state"], "running")
        route, leg_path = self.leg()
        self.finish_leg(route, leg_path)
        before = self.snapshot()
        again = self.replay()
        self.assertEqual(again["state"], "completed", again)
        self.assertEqual(again["required_action"], "advance-completed")
        self.assertNotEqual(again.get("parent_next"), "end-turn")
        self.untouched(before)

    def test_a_staged_leg_whose_owner_still_runs_answers_running_without_a_second_launch(self):
        first = self.staged_leg()
        before = self.snapshot()
        again = self.replay()
        self.assertEqual(again["state"], "running", again)
        self.assertEqual(again["owner_attempt_id"], first["owner_attempt_id"])
        self.assertEqual(again["resume_command"], first["resume_command"])      # the leg's, not the frame's
        self.untouched(before)

    def test_a_staged_leg_whose_owner_died_is_reported_and_not_relaunched_by_the_replay(self):
        first = self.staged_leg()
        self.end_owner_row()
        before = self.snapshot()
        with mock.patch("dispatch_replacement.advance", side_effect=AssertionError("a replay never launches")), \
                mock.patch.object(W, "_launch_admitted", side_effect=AssertionError("a replay never launches")):
            again = self.replay()
        self.assertEqual(again["state"], "needs-attention", again)
        self.assertEqual(again["owner_attempt_id"], first["owner_attempt_id"])
        self.assertEqual(again["resume_command"], first["resume_command"])
        self.untouched(before)

    def test_a_staged_leg_parked_at_a_human_gate_answers_the_gate_not_running(self):
        first = self.staged_leg()
        parked = {"gate": "preview-disposition", "status": "blocked", "artifact": "/tmp/preview.md",
                  "route_file": str(self.leg()[1])}
        self.end_owner_row()
        before = self.snapshot()
        with mock.patch("dispatch_replacement.owner_parked_gate", return_value=parked):
            again = self.replay()
        self.assertEqual((again["state"], again["required_action"], again["gate"]),
                         ("waiting-human-gate", "answer-human-gate", "preview-disposition"), again)
        self.assertIn("release_command", again)
        self.untouched(before)

    def test_a_replay_of_an_exited_leg_owner_asks_the_approval_question_it_never_raised(self):
        # The frame route's own resume is a parent `start` too: an exited owner that never raised the gate
        # sealed on its own operation is asked about here as well, and still nothing launches or settles.
        first = self.staged_leg()
        self.end_owner_row()
        before = self.snapshot()
        parked = {"gate": "preview-disposition", "status": "blocked", "artifact": "/tmp/preview.md",
                  "route_file": str(self.leg()[1])}
        with mock.patch.object(W, "_raise_owner_entry_gate") as raised, \
                mock.patch("dispatch_replacement.owner_parked_gate",
                           side_effect=lambda *a, **k: parked if raised.called else None), \
                mock.patch("dispatch_replacement.advance", side_effect=AssertionError("a replay never launches")), \
                mock.patch.object(W, "_launch_admitted", side_effect=AssertionError("a replay never launches")):
            again = self.replay()
        raised.assert_called_once()
        self.assertEqual(raised.call_args.args[3], first["owner_attempt_id"])
        self.assertEqual((again["state"], again["required_action"], again["gate"]),
                         ("waiting-human-gate", "answer-human-gate", "preview-disposition"), again)
        self.assertEqual(again["resume_command"], first["resume_command"])
        self.untouched(before)

    def test_a_replay_of_a_leg_whose_owner_still_runs_asks_nothing(self):
        self.staged_leg()
        with mock.patch.object(W, "_raise_owner_entry_gate", side_effect=AssertionError("owner is live")):
            self.assertEqual(self.replay()["state"], "running")

    def test_an_inline_finish_still_pending_on_the_leg_is_reported_as_such(self):
        self.settle()
        route, _ = self.leg()
        state = self.root / ".runtime" / "inline-finish" / "v1" / route["route_id"] / "finish.json"
        state.parent.mkdir(parents=True)
        state.write_text(json.dumps({"schema": "inline_finish_v1", "state": "node-completed"}), encoding="utf-8")
        before = self.snapshot()
        again = self.replay()
        self.assertEqual((again["state"], again["reason"]), ("needs-attention", "finish-pending"), again)
        self.untouched(before)

    def test_a_direct_leg_that_is_still_to_be_done_keeps_answering_execute_inline(self):
        first = self.settle()
        before = self.snapshot()
        again = self.replay()
        self.assertEqual({k: v for k, v in again.items() if k != "route_decision"},
                         {k: v for k, v in first.items() if k != "route_decision"})
        self.untouched(before)

    def test_a_first_leg_that_is_not_the_one_the_record_binds_is_a_conflict_not_a_state(self):
        self.settle()
        route, leg_path = self.leg()
        forged = {**route, "cwd": str(self.root)}
        leg_path.write_text(json.dumps(forged, indent=2) + "\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            self.replay()


class PinnedStartBase(StartBase):
    """A framed route composed with the pins the CLI would seal (`--pin` after `_filter_top_pins`)."""

    PINS = ["owner=claude", "worker=claude:sonnet@high", "frame=claude"]
    OWNER_HARNESS = "claude"

    def compose(self, **kw):
        pins, _ = R._filter_top_pins(R._parse_selection_pins(self.PINS))
        kw.setdefault("selection_pins", pins)
        kw.setdefault("work_request", {"text": "Decide how to do this", "owner_harness": self.OWNER_HARNESS})
        return super().compose(**kw)

    def pins_sealed_on_frame(self):
        return {k: v for k, v in self.route["selection_pins"].items() if k != "contract_version"}


class SelectionPinInheritanceTest(PinnedStartBase):
    """The compose-time pins reach the leg the frame proposes and starts (defect 1)."""

    def first_leg(self):
        (leg_path,) = self.leg_routes()
        return json.loads(leg_path.read_text(encoding="utf-8"))

    def test_start_pin_changes_reach_the_selected_leg_and_owner_launch(self):
        import route_authority as RA
        self.set_briefs(CODE_STAGED, CODE_STAGED)
        self.set_interview({"legs": [CODE_STAGED]})
        before = self.path.read_bytes()
        with patch_split(R, "_pin_change_probe", return_value=([], [])), \
                mock.patch.object(RA, "caller_identity", return_value=("claude", "parent")):
            changed = R._change_pins(self.route, self.jobs, ["owner=codex", "worker=codex"])
        self.assertTrue(changed["changed"])
        # The frame checked Claude; composing the pinned Codex leg checks its
        # conductor too. Keep that probe independent of installed CLIs.
        checked = {"tuples": [T.nested("codex", "codex")],
                   "candidates": T.registered_headless()["candidates"]}
        with patch_split(R, "_compose_readiness", return_value=checked) as probe:
            result = self.settle()
        self.assertEqual(probe.call_args.args[2], "codex")
        leg = self.first_leg()
        self.assertIn(checked["tuples"][0], leg["dispatch_evidence"]["tuples"])
        self.assertEqual(leg["selection_pins"]["owner"]["harness"], "codex")
        self.assertEqual(leg["selection_pins"]["worker"]["harness"], "codex")
        self.assertEqual(leg["selection_pins"]["frame"]["harness"], "claude")
        self.assertEqual(leg["work_request"]["owner_harness"], "codex")
        self.assertEqual({n["harness_affinity"] for n in leg["nodes"] if n.get("dispatch_depth") == 2}, {"codex"})
        self.assertEqual(len(self.leg_calls), 1, result)
        command = self.leg_calls[-1]
        self.assertEqual(command[command.index("--adapter") + 1], "codex", result)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(RP.frame_selection_pins(self.record()["decision"], self.root),
                         RA.selection_pin_rows(self.route))

    def test_the_first_staged_leg_seals_the_frames_pins_and_every_worker_follows_the_worker_pin(self):
        self.set_briefs(CODE_STAGED, CODE_STAGED)
        self.set_interview({"legs": [CODE_STAGED]})
        self.settle()
        leg = self.first_leg()
        self.assertEqual(leg["selection_pins"], self.route["selection_pins"])
        workers = [n for n in leg["nodes"] if n.get("dispatch_depth") == 2]
        self.assertTrue(workers)
        self.assertEqual({n["harness_affinity"] for n in workers}, {"claude"})
        for node in workers:
            self.assertEqual(node["harness_policy"]["primary"][0], "claude", node["id"])
        self.assertEqual(leg["work_request"]["owner_harness"], "claude")
        R.verify_route(json.loads(json.dumps(leg)), R.ROOT)

    def test_a_direct_first_leg_and_a_solo_first_leg_carry_the_pins_too(self):
        for leg_arguments in (DIRECT, {"capability": "autopilot-code", "shape": "solo", "why": "one worker"}):
            with self.subTest(shape=leg_arguments["shape"]):
                self.tearDown()
                self.setUp()
                self.set_briefs(leg_arguments, leg_arguments)
                self.set_interview({"legs": [leg_arguments]})
                self.settle()
                self.assertEqual(self.first_leg()["selection_pins"], self.route["selection_pins"])

    def test_a_pin_the_user_never_gave_adds_no_field_to_the_leg(self):
        self.tearDown()
        self.PINS, self.OWNER_HARNESS = [], None
        self.setUp()
        self.set_briefs(CODE_STAGED, CODE_STAGED)
        self.set_interview({"legs": [CODE_STAGED]})
        self.settle()
        self.assertNotIn("selection_pins", self.route)
        leg = self.first_leg()
        self.assertNotIn("selection_pins", leg)
        self.assertEqual({n.get("harness_affinity") for n in leg["nodes"] if n.get("dispatch_depth") == 2}, {"diverse"})

    def test_a_frame_only_pin_is_sealed_on_the_leg_without_touching_worker_affinity(self):
        self.tearDown()
        self.PINS, self.OWNER_HARNESS = ["frame=claude"], None
        self.setUp()
        self.set_briefs(CODE_STAGED, CODE_STAGED)
        self.set_interview({"legs": [CODE_STAGED]})
        self.settle()
        leg = self.first_leg()
        self.assertEqual(leg["selection_pins"], self.route["selection_pins"])
        self.assertEqual({n["harness_affinity"] for n in leg["nodes"] if n.get("dispatch_depth") == 2}, {"diverse"})

    def test_the_owner_pin_reaches_the_first_legs_work_request_even_when_the_frames_request_did_not_name_it(self):
        self.tearDown()
        self.OWNER_HARNESS = None
        self.setUp()
        self.set_briefs(CODE_STAGED, CODE_STAGED)
        self.set_interview({"legs": [CODE_STAGED]})
        self.settle()
        self.assertEqual(self.first_leg()["work_request"]["owner_harness"], "claude")

    def test_the_leg_gets_its_own_copy_of_the_pins_and_the_frame_route_is_not_changed(self):
        before = json.dumps(self.route["selection_pins"], sort_keys=True)
        compiled = R._leg_compose_kwargs(RP.leg_arguments(CODE_STAGED), frame_route=self.route,
                                         frame_cycle_id="cyc_" + "a" * 32, slug="x")
        pins = compiled["selection_pins"]
        self.assertEqual(pins, self.pins_sealed_on_frame())
        self.assertNotIn("contract_version", pins)
        pins["worker"]["harness"] = "codex"
        self.assertEqual(json.dumps(self.route["selection_pins"], sort_keys=True), before)

    def test_the_readiness_probe_asks_about_a_pinned_harness_even_if_the_user_policy_left_it_out(self):
        seen = []
        with patch_split(R, "_compose_default_children", return_value=("codex",)), \
                patch_split(R, "_compose_readiness",
                                  side_effect=lambda *a, **kw: seen.append((a[3], kw.get("owner_harnesses"))) or {"tuples": [], "candidates": []}):
            REAL_PROPOSAL_READINESS(self.route, self.jobs)
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0][1], ("codex",))
        with patch_split(R, "_compose_default_children", wraps=R._compose_default_children) as children, \
                patch_split(R, "_compose_readiness", return_value={"tuples": [], "candidates": []}):
            REAL_PROPOSAL_READINESS(self.route, self.jobs)
        self.assertIn(mock.call(self.pins_sealed_on_frame()), children.call_args_list)


class CatalogueInputTest(F.FramedBase):
    def test_a_frame_leg_gets_the_request_the_hints_and_the_full_catalogue_from_the_same_source_as_stages(self):
        route = self.compose(capability="autopilot-spec", graph="plan,execute",
                             work_request={"text": "Make the thing", "owner_harness": None})
        with mock.patch("subprocess.run", side_effect=AssertionError("the catalogue is never read back from CLI text")):
            text = W.frame_task_text(route)
        self.assertTrue(text.startswith("Make the thing\n"))
        self.assertIn("- capability: autopilot-spec", text)
        self.assertIn("- graph: plan,execute", text)
        block = text.split("```json\n", 1)[1].split("\n```", 1)[0]
        registry = R.TOPO.load_registry()
        expected = [R.stages_block(registry, r) for r in registry["recipes"] if r["capability"] != "route-frame"]
        self.assertEqual(json.loads(block), json.loads(json.dumps(expected)))
        self.assertIn("autopilot-lab", block)
        self.assertIn('"start_approval":"full-run"', block)
        self.assertNotIn("route-frame", block)
        self.assertIn("do not read another leg's brief", text)
        self.assertEqual(text, W.frame_task_text(route))

    def test_recipe_internal_and_quick_frames_get_the_same_unit_prompt_and_catalogue(self):
        for shape, graph in (("staged", None), ("solo", None)):
            # A recipe's own frame pair: as a preset compile seals it (and compose did before decided shapes).
            with self.subTest(shape), patch_split(R, "DECIDED_SHAPES", ()):
                route = R.compose_route(
                    capability="autopilot-code", capability_mode=None, shape=shape, graph=graph, slug="frames",
                    cwd=R.ROOT, artifact_root=self.root, spec_read="fixture", campaign_key="k",
                    dispatch_evidence={"tuples": [T.nested("claude", "codex")]},
                    registered_headless_evidence=T.registered_headless(),
                    work_request={"text": "Fix it", "owner_harness": None})
                path = Path(L.admit_runtime_route(self.root, route).route_file)
                seen = []

                def run(command, **kw):
                    seen.append(command)
                    return subprocess.CompletedProcess(command, 1, "", "")
                W._start(route, path, self.jobs, "frame", None, run)
                command = seen[0]
                self.assertNotIn("--prompt-text", command)
                prompt = Path(command[command.index("--prompt-file") + 1]).read_text(encoding="utf-8")
                self.assertTrue(prompt.startswith("Fix it\n"))
                self.assertIn("## Part catalogue", prompt)
                self.assertEqual(route["nodes"][0]["unit"], "plan/frame")
        owner = []
        W._start(route, path, self.jobs, "owner", None, lambda command, **kw: owner.append(command) or subprocess.CompletedProcess(command, 1, "", ""))
        self.assertIn("--prompt-text", owner[0])                      # an owner still gets the request itself

    def test_both_legs_read_one_file_so_neither_sees_the_other(self):
        route, path = self.admitted()
        commands = []
        for node in F.FRAME_IDS:
            W._start(route, path, self.jobs, node, None, lambda command, **kw: commands.append(command) or subprocess.CompletedProcess(command, 1, "", ""))
        files = {c[c.index("--prompt-file") + 1] for c in commands}
        self.assertEqual(len(files), 1)
        self.assertIn("do not read another leg's brief", Path(files.pop()).read_text())


class ReviewStartTest(F.FramedStartTest):
    """`needs-interview` for a framed route carries the two validated proposals as information."""

    def test_the_interview_step_gets_both_proposals_their_equality_and_the_downgrade_place(self):
        self.start()
        self.ready = True
        self.steps = [{"state": "needs-interview", "required_action": "prepare-frame-question", "next_step": "go"}]
        output = Path(P.list_cycle_records(self.root)[0]["cycle_id"]) and None
        record = P.list_cycle_records(self.root)[0]
        cycle = P.cycle_dir(self.root, record["campaign_id"], record["cycle_id"], record) / "artifacts"
        (cycle / "shards/frame-alternative").mkdir(parents=True, exist_ok=True)
        (cycle / "shards/frame").mkdir(parents=True, exist_ok=True)
        (cycle / "shards/frame/direction-brief.md").write_text(brief([DIRECT]), encoding="utf-8")
        (cycle / "shards/frame-alternative/direction-brief.md").write_text("## 8. 경로 조립 제안\n\nnone\n", encoding="utf-8")
        with mock.patch.object(R, "proposal_readiness", side_effect=AssertionError("a direct leg needs no probe")):
            W._ROUTE_MODULE = R
            self.addCleanup(setattr, W, "_ROUTE_MODULE", None)
            result = self.start()
        self.assertEqual(result["state"], "needs-interview")
        review = result["route_proposal_review"]
        self.assertEqual([row["node"] for row in review["proposals"]], F.FRAME_IDS)
        first, second = review["proposals"]
        self.assertEqual((first["reason"], first["display"]), ("valid", "proposal"))
        self.assertEqual(first["legs"][0]["shape"], "direct")
        self.assertEqual((second["proposal"], second["reason"], second["display"]),
                         (None, "block-missing", "proposal:none(block-missing)"))
        self.assertEqual((review["equal"], review["wording_differs"]), (False, False))
        self.assertIn("frame_downgrade", review)
        self.assertIsNone(review["frame_downgrade"])                  # nothing ran lower
        self.assertIn("\"proposal\" and \"approves\" marks", result["next_step"])
        route_question, = result["interview_template"]["questions"]      # only the valid brief is offered
        self.assertEqual([o.get(FI.PROPOSAL_MARK) for o in route_question["options"]], ["frame", None])
        self.assertEqual(result["frame_interview"]["route_proposal_review"], review)
        self.assertEqual(len(self.calls), 2)                          # nothing launched to learn this

    def test_a_frame_leg_retried_one_profile_lower_is_named_in_the_result_and_the_review(self):
        self.start()
        self.ready = True
        self.steps = [{"state": "needs-interview", "required_action": "prepare-frame-question", "next_step": "go"}]
        record = P.list_cycle_records(self.root)[0]
        cycle = P.cycle_dir(self.root, record["campaign_id"], record["cycle_id"], record) / "artifacts"
        for node in F.FRAME_IDS:
            (cycle / "shards" / node).mkdir(parents=True, exist_ok=True)
            (cycle / "shards" / node / "direction-brief.md").write_text(brief([DIRECT]), encoding="utf-8")
        summary = [{"node": "frame", "original_profile": "top", "actual_profile": "deep", "cause": "capacity",
                    "attempt_id": "att-replacement", "original_attempt_id": "att-source"}]
        W._ROUTE_MODULE = R
        self.addCleanup(setattr, W, "_ROUTE_MODULE", None)
        with mock.patch.object(W, "_frame_downgrade_summary", return_value=summary) as summarize:
            result = self.start()
        self.assertEqual(result["state"], "needs-interview")
        self.assertEqual(result["frame_downgrade"], summary)                       # the frame summary
        self.assertEqual(result["route_proposal_review"]["frame_downgrade"], summary)   # the interview result
        self.assertTrue(all(call.args[0]["route_id"] == result["route_id"] for call in summarize.call_args_list))
        self.ready = True
        with mock.patch.object(W, "_frame_downgrade_summary", return_value=None):
            quiet = self.start()
        self.assertNotIn("frame_downgrade", quiet)                                  # no downgrade, no key
        self.assertIsNone(quiet["route_proposal_review"]["frame_downgrade"])

    def test_a_failed_frame_never_lets_the_surviving_brief_decide(self):
        self.start()
        self.ready = True

        def one_failed(jobs, aid, **kw):
            from dispatch_completion_join import CurrentDeliveryState
            failed = aid.endswith(self.failed_suffix)
            return CurrentDeliveryState(marker=None if failed else {"artifact": "/b.md"}, marker_digest="" if failed else "sha256:m",
                                        row_revision="1", row_digest="sha256:r", status="done",
                                        verdict="FAIL" if failed else "PASS", quiescent=True, owned_children=0,
                                        advanced=False, completion_proven=not failed)
        self.failed_suffix = self.calls[1][self.calls[1].index("--attempt-id") + 1][-4:]
        with mock.patch.object(W, "current_delivery_state", side_effect=one_failed):
            result = self.start()
        self.assertEqual((result["state"], result["reason"]), ("needs-attention", "frame-outcome-needs-inspection"))
        self.assertFalse(R.outcome_path(self.path).exists())
        self.assertFalse(list(self.root.rglob("route-decision.json")))
        self.assertNotIn("route_proposal_review", result)


# Inherited tests belong to the framed-route suite; only this suite's own run here.
for _name in dir(F.FramedStartTest):
    if _name.startswith("test_") and _name not in ReviewStartTest.__dict__:
        setattr(ReviewStartTest, _name, None)


def full_brief(legs, *, questions="없음. 범위와 방향은 요청으로 정해졌다.", **kw):
    """A brief with the sections the interview brief is drafted from, and section 7 as given."""
    return brief(legs, **kw).replace(
        "## 7. Questions\n\nnone\n",
        "## 4. Direction Verdict\n\nGo direct.\n\n## 5. Hotfix Boundary\n\nNo shortcut.\n\n"
        "## 7. Questions\n\n" + questions + "\n")


class AutomaticDirectionTest(StartBase):
    """Frame review asks only when the two legs diverge or leave a question; otherwise the runtime
    records the same interview with actor_kind automatic (WORKFLOW §0.4)."""

    def automatic(self):
        root, record, output = W._framed_cycle(self.route)
        return W._automatic_direction(self.route, W._proposal_rows(self.route, self.jobs, root, record, output), output)

    def test_equal_proposals_with_nothing_to_choose_get_a_valid_runtime_interview(self):
        self.set_briefs(full_brief([DIRECT]), full_brief([DIRECT], questions="none"))
        interview, answers = self.automatic()
        derived, errors = W._marked_route_proposals(self.route, self.jobs, interview)
        self.assertEqual(errors, [])
        self.assertEqual(FI.validate({**derived, "summary": "-"}), [])
        self.assertEqual(list(derived["route_proposals"]["by_option"]), ["진행"])
        self.assertEqual(derived["route_proposals"]["by_option"]["진행"]["legs"][0]["shape"], "direct")
        self.assertEqual((answers["actor_kind"], answers["basis"]), ("supervisor", FI.AUTOMATIC_BASIS))
        self.assertEqual(FI.validate_answers(derived, answers), [])
        self.assertFalse(FI.answer_releases_gate("automatic", "frame-review"))   # no new release source
        with self.assertRaisesRegex(ValueError, "registered worker cannot claim supervisor"):
            FI.answer_actor_kind(answers, registered_worker=True)

    def test_a_question_left_a_divergence_an_approval_or_a_worker_caller_keeps_the_person_asked(self):
        cases = {
            "question": (full_brief([DIRECT]), full_brief([DIRECT], questions="1. 이번에 같이 고칠까요?")),
            "no-section": (full_brief([DIRECT]), brief([DIRECT]).replace("## 7. Questions\n\nnone\n", "")),
            "diverged": (full_brief([DIRECT]), full_brief([CODE_STAGED])),
            "approval": (full_brief([LAB_SETUP]), full_brief([LAB_SETUP])),
            "thin-brief": (brief([DIRECT]), brief([DIRECT])),
        }
        for name, briefs in cases.items():
            with self.subTest(name):
                self.set_briefs(*briefs)
                self.assertIsNone(self.automatic())
        self.set_briefs(full_brief([DIRECT]), full_brief([DIRECT]))
        self.assertIsNotNone(self.automatic())
        for node in F.FRAME_IDS:
            with self.subTest("brief-missing", node=node):
                # The decision opens only with both briefs (U4); a missing one is never agreed for the person.
                self.set_briefs(full_brief([DIRECT]), full_brief([DIRECT]))
                (self.output / "shards" / node / "direction-brief.md").unlink()
                self.assertIsNone(self.automatic())
        self.set_briefs(full_brief([DIRECT]), full_brief([DIRECT]))
        with mock.patch.dict(os.environ, {"AGENT_DISPATCH_REGISTERED_WORKER": "1"}):
            self.assertIsNone(self.automatic())

    def test_an_agreed_direction_past_the_intents_not_doing_is_asked(self):
        summary = "frame-review에 새 게이트를 추가한다"
        self.set_briefs(full_brief([DIRECT], summary=summary), full_brief([DIRECT], summary=summary))
        self.assertIsNotNone(self.automatic())
        self.route = {**self.route, "work_request": {**self.route["work_request"],
                                                     "text": "안 할 것: 새 게이트·필수 입력\n\n" + self.route["work_request"]["text"]}}
        self.assertIsNone(self.automatic())          # both legs agree, but only the person may choose it

    def test_section_seven_reads_none_in_either_language_and_anything_else_as_a_question(self):
        for body, left in (("없음.", False), ("없다(0건).", False), ("0개 — 모두 코드로 정한다.", False),
                           ("None.", False), ("", False), ("1. 지울까요?", True), ("없음을 확인할 질문", True)):
            with self.subTest(body=body):
                self.assertEqual(FI.user_questions_left(f"## 7. Questions\n\n{body}\n\n## 8. Next\n\nx\n"), left)
        self.assertTrue(FI.user_questions_left("## 6. Risks\n\nx\n"))


class AutomaticDirectionStartTest(F.FramedStartTest):
    """The start that reaches the interview releases it itself, once, through the real settle path."""

    def test_nothing_to_choose_releases_through_the_runtime_and_settles_for_real(self):
        self.start()
        self.ready = True
        record = P.list_cycle_records(self.root)[0]
        cycle = P.cycle_dir(self.root, record["campaign_id"], record["cycle_id"], record) / "artifacts"
        for node in F.FRAME_IDS:
            (cycle / "shards" / node).mkdir(parents=True, exist_ok=True)
            (cycle / "shards" / node / "direction-brief.md").write_text(full_brief([DIRECT]), encoding="utf-8")
        seen = []

        def step(route, path, jobs, **kw):
            seen.append(kw)
            if kw.get("automatic"):
                self.released = True
                return {"state": "released", "decision": "proceed"}
            return {"state": "needs-interview", "required_action": "prepare-frame-question", "next_step": "go"}
        W.frame_interview_step.side_effect = step
        W._ROUTE_MODULE = R
        self.addCleanup(setattr, W, "_ROUTE_MODULE", None)
        settled = []
        real_settle = W._framed_settle

        def settle(*args, **kw):
            settled.append(args[0]["route_id"])
            return real_settle(*args, **kw)
        with mock.patch.object(W, "_framed_settle", side_effect=settle):
            result = self.start()
        self.assertEqual([bool(kw.get("automatic")) for kw in seen], [False, True])
        automatic = seen[1]
        self.assertEqual(json.loads(Path(automatic["answers"]).read_text())["basis"], FI.AUTOMATIC_BASIS)
        self.assertTrue(str(automatic["interview"]).startswith(str(cycle / W.AUTOMATIC_DIRECTION_DIR)))
        self.assertEqual(settled, [self.route["route_id"]])          # the real settle ran
        self.assertIn("state", result)
        self.assertEqual(len(self.calls), 2)                          # no question was asked


class AutomaticDirectionNoticeTest(StartBase):
    """The first leg's receipt -- the one the parent actually gets -- carries the notice, every time."""

    def recorded(self, *, actor_kind="supervisor", basis=FI.AUTOMATIC_BASIS):
        self.set_briefs(full_brief([DIRECT]), full_brief([DIRECT]))
        root, record, output = W._framed_cycle(self.route)
        interview, answers = W._automatic_direction(
            self.route, W._proposal_rows(self.route, self.jobs, root, record, output), output)
        self.interview, _ = W._marked_route_proposals(self.route, self.jobs, interview)
        self.answers = {**answers, "actor_kind": actor_kind}
        if basis is None:
            self.answers.pop("basis")

    def test_the_started_leg_receipt_and_its_replay_carry_the_direction_notice(self):
        self.recorded()
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            first = self.settle()
        self.assertTrue(first["direction_notice"].startswith("[방향 통지]"))   # this suite declares Korean
        self.assertIn("Do the work", first["direction_notice"])
        self.assertTrue(first["next_step"].startswith(W.DIRECTION_NOTICE_STEP))
        self.assertIn(first["direction_notice"], stderr.getvalue())
        self.assertEqual(len(self.leg_routes()), 1)                     # the leg really started
        replay = self.settle()
        self.assertEqual(replay["direction_notice"], first["direction_notice"])

    def declare(self, language):
        """The operator's language on an isolated preference path; None is no declaration (English)."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        env = {"HOME": tmp.name, "XDG_CONFIG_HOME": tmp.name + "/xdg", "CLAUDE_CONFIG_DIR": tmp.name + "/claude"}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("FLEET_NOW_LANG", None)
        if language:
            os.environ["FLEET_NOW_LANG"] = language

    def test_the_notice_is_english_by_default_and_korean_when_declared(self):
        self.recorded()
        self.declare(None)
        english = W._automatic_direction_notice_text(self.interview, self.answers)
        self.assertEqual(english, "[Direction notice] The two review legs agreed on a direction and left nothing "
                                  "to ask, so work started without asking: Do the work. If anything needs fixing, "
                                  "let me know in your next message.")
        self.assertIsNone(re.search(r"[\uac00-\ud7a3]", english))
        self.declare("ko")
        self.assertEqual(W._automatic_direction_notice_text(self.interview, self.answers),
                         "[방향 통지] 두 갈래 검토가 같은 방향을 내고 물을 것을 남기지 않아 묻지 않고 시작했습니다"
                         ": Do the work. 고칠 점이 있으면 다음 말로 알려 주세요.")

    def test_a_proposal_without_a_summary_uses_the_catalog_wording_for_the_direction(self):
        for language, means in ((None, "Start in the direction both legs agreed on"),
                                ("ko", "두 갈래가 함께 낸 방향대로 시작합니다")):
            with self.subTest(language=language):
                self.declare(language)
                self.set_briefs(full_brief([DIRECT]), full_brief([DIRECT]))
                root, record, output = W._framed_cycle(self.route)
                rows = W._proposal_rows(self.route, self.jobs, root, record, output)
                for row in rows:
                    row["proposal"] = {**row["proposal"], "summary": ""}
                interview, answers = W._automatic_direction(self.route, rows, output)
                self.assertEqual(interview["questions"][0]["options"][0]["means"], means)
                self.assertIn(means, W._automatic_direction_notice_text(interview, answers))

    def test_a_persons_answer_starts_the_leg_without_a_notice(self):
        self.recorded(actor_kind="user", basis=None)
        result = self.settle()
        self.assertNotIn("direction_notice", result)
        self.assertNotIn(W.DIRECTION_NOTICE_STEP, str(result.get("next_step") or ""))


for _name in dir(F.FramedStartTest):
    if _name.startswith("test_") and _name not in AutomaticDirectionStartTest.__dict__:
        setattr(AutomaticDirectionStartTest, _name, None)


class NonFramedClosedRouteTest(T.ProducerTestBase):
    """A closed direct route keeps its values; only the observation receipt reference is additive."""

    def test_a_closed_direct_route_replays_completed_with_its_old_fields_and_no_framed_or_plan_fields(self):
        with mock.patch.dict(os.environ, F.clean_environment(), clear=True):
            os.environ.update(AGENT_HOME=str(R.ROOT), AGENT_DISPATCH_JOBS=str(self.jobs))
            self.activate()
            route = R.compose_route(
                capability="autopilot-code", capability_mode=None, shape="direct", graph=None, slug="plain-direct",
                cwd=R.ROOT, artifact_root=self.root, spec_read="fixture", campaign_key="plain-key",
                work_request={"text": "Do the small thing", "owner_harness": None})
            route_file = Path(L.admit_runtime_route(self.root, route).route_file)
            self.close(route, route_file)
            first = W.start_work(route, route_file, self.jobs)
            again = W.start_work(route, route_file, self.jobs)
        self.assertEqual(first, again)
        self.assertEqual(set(first) - {"receipt_file"}, {"route_file", "route_id", "launches", "owner_started", "advisories",
                                      "resume_command", "state", "required_action", "outcome"})
        self.assertEqual(Path(first["receipt_file"]).parent, self.jobs.parent / "start-receipts")
        self.assertEqual((first["state"], first["required_action"]), ("completed", "advance-completed"))
        self.assertIs(first["outcome"]["terminal_gate_proven"], True)
        for new in ("next_leg", "route_decision", "record_file", "selected"):
            self.assertNotIn(new, first)


# --- Interruption: a real process is killed at a boundary, then the same settle runs in a new one ---

CRASH_EXIT = 77
BOUNDARIES = ("after-intent-save", "after-decision-write", "after-compiled-snapshot", "after-route-publish",
              "after-route-bind", "run-before-row", "run-after-row", "after-first-start",
              "after-first-start-receipt", "after-complete", "after-close", "after-manifest",
              "after-finalize")


def fixture_git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True).stdout.strip()


def checkout_with_origin(base):
    """A primary checkout with its own bare origin, whose default branch is one commit ahead.

    A source-changing leg prepares its worktree from this origin in whichever process runs it,
    never from the checkout running the tests. Returns `(primary, remote tip SHA)`."""
    primary, origin, writer = base / "repo", base / "origin.git", base / "writer"
    identity = ("-c", "user.name=Fixture", "-c", "user.email=fixture@example.test")
    fixture_git(base, "init", "-q", "--bare", "-b", "main", str(origin))
    fixture_git(base, "init", "-q", "-b", "main", str(primary))
    (primary / "base.txt").write_text("base\n", encoding="utf-8")
    fixture_git(primary, "add", "base.txt")
    fixture_git(primary, *identity, "commit", "-qm", "base")
    fixture_git(primary, "remote", "add", "origin", str(origin))
    fixture_git(primary, "push", "-q", "origin", "main")
    fixture_git(base, "clone", "-q", str(origin), str(writer))
    (writer / "remote-tip.txt").write_text("remote tip\n", encoding="utf-8")
    fixture_git(writer, "add", "remote-tip.txt")
    fixture_git(writer, *identity, "commit", "-qm", "remote tip")
    fixture_git(writer, "push", "-q", "origin", "main")
    return primary, fixture_git(writer, "rev-parse", "HEAD")


class OwnCheckout:
    """Mixin: the frame sits in its own checkout with its own origin, so real worktree preparation,
    in this process or a child one, happens there and never in the checkout running the tests."""

    def setUp(self):
        source = tempfile.TemporaryDirectory()
        self.addCleanup(source.cleanup)
        self.primary, self.remote_sha = checkout_with_origin(Path(source.name))
        self.primary_head = fixture_git(self.primary, "rev-parse", "HEAD")
        super().setUp()

    def compose(self, **kwargs):
        kwargs["cwd"] = self.primary
        kwargs["dispatch_evidence"] = {"tuples": [
            {**T.nested("claude", "codex"), "checked_worktree": str(self.primary.resolve())}]}
        return super().compose(**kwargs)


def driver(context_path, crash_at):
    """One `_framed_settle` in this process with the fixture's controls; `crash_at` kills it there."""
    context = json.loads(Path(context_path).read_text(encoding="utf-8"))
    os.environ.clear()
    os.environ.update(context["env"])
    jobs, path = Path(context["jobs"]), Path(context["route_file"])
    route = json.loads(path.read_text(encoding="utf-8"))
    W._ROUTE_MODULE = R

    def hook(name):
        if crash_at == "after-manifest" and name == "finalize-crash-after-manifest":
            return True
        if name == crash_at:
            os._exit(CRASH_EXIT)
    W.FAULT_HOOK = hook

    def in_process_cli(_jobs, *argv):
        raw = json.loads(path.read_text(encoding="utf-8"))
        if argv[0] == "complete":
            verified = R.verify_route(raw)
            node = next(n for n in verified["nodes"] if n["id"] == argv[argv.index("--node") + 1])
            R.complete_node(verified, node, node["id"], Path(argv[argv.index("--evidence") + 1]))
        else:
            R.close_route(R.verify_route(raw, allow_stale_registry=True), path,
                          summary=argv[argv.index("--summary") + 1], allow_unproven=False)
        return ""

    def run(command, **kwargs):
        if crash_at == "run-before-row":
            os._exit(CRASH_EXIT)
        value = lambda flag: command[command.index(flag) + 1]
        leg = json.loads(Path(value("--route-evidence")).read_text(encoding="utf-8"))
        meta = {"attempt_id": value("--attempt-id"), "parent_sid": "parent", "launch_started": "1",
                "worker_type": "owner", "dispatch_depth": "1", "route_id": leg["route_id"],
                "route_hash": leg["route_hash"], "owner_route_id": leg["route_id"],
                "owner_route_hash": leg["route_hash"], "parent_completion_delivery": "codex-managed-gateway"}
        with jobs.open("a") as stream:
            stream.write("now\topen\t12\tparent\ttask\t" + ",".join(k + "=" + v for k, v in meta.items()) + "\n")
        if crash_at == "run-after-row":
            os._exit(CRASH_EXIT)
        return subprocess.CompletedProcess(command, 0, "registered=1 started=1 child_spawned=1\n", "")

    def readiness(probed_route, _jobs):
        # Fixed evidence, bound to the exact cwd probed: the prepared worktree the leg seals.
        checked = str(Path(probed_route["cwd"]).resolve())
        return {"tuples": [{**T.nested("claude", "codex"), "checked_worktree": checked}],
                "candidates": T.registered_headless()["candidates"]}

    real_prepare = R.prepare_isolated_worktree

    def prepare(cwd, slug, **kwargs):
        target, repository = str(Path(cwd).resolve()), str(R.ROOT.resolve())
        if os.path.commonpath([target, repository]) == repository:
            raise AssertionError("the driver reached the checkout running the tests: " + target)
        return real_prepare(cwd, slug, **kwargs)

    interview = json.loads(Path(context["interview"]).read_text(encoding="utf-8"))
    answers = json.loads(Path(context["answers"]).read_text(encoding="utf-8"))
    with split_patch("dispatch_contract.completion_marker_gate"), \
            mock.patch.object(W, "_route_cli", side_effect=in_process_cli), \
            mock.patch.object(R, "proposal_readiness", side_effect=readiness), \
            mock.patch.object(R, "prepare_isolated_worktree", side_effect=prepare), \
            mock.patch.object(W, "_recorded_interview", return_value=(interview, answers)), \
            mock.patch.object(W, "default_parent_session_id", return_value="parent"), \
            mock.patch.object(W, "join_selected_attempts", return_value={"state": "timeout", "children": []}), \
            mock.patch("route_parent_close.owner_continuation_processes", side_effect=owner_observation), \
            mock.patch.object(W, "parent_next", return_value=("end-turn", "fixture", "")):
        result = W._framed_settle(route, path, jobs, {"route_id": route["route_id"]}, run=run)
    print("RESULT " + json.dumps(result, sort_keys=True))
    return 0


class InterruptionTest(OwnCheckout, StartBase):
    """A164-6: stop at each boundary of the transaction, run the same start again in a fresh process,
    and read the registry, route files, record and manifest back from disk. The fresh process
    prepares the source leg's worktree for real, in the frame's own checkout."""

    def setUp(self):
        super().setUp()
        self.set_briefs(CODE_STAGED, CODE_STAGED)
        self.set_interview({"legs": [CODE_STAGED]})
        base = Path(self._tmp.name)
        (base / "interview.json").write_text(json.dumps(self.interview), encoding="utf-8")
        (base / "answers.json").write_text(json.dumps(self.answers), encoding="utf-8")
        self.context = base / "context.json"
        self.context.write_text(json.dumps({
            "env": dict(os.environ), "jobs": str(self.jobs), "route_file": str(self.path),
            "interview": str(base / "interview.json"), "answers": str(base / "answers.json")}), encoding="utf-8")

    def run_driver(self, crash_at=""):
        return subprocess.run([sys.executable, str(Path(__file__).resolve()), "--driver", str(self.context), crash_at],
                              text=True, capture_output=True, env=dict(os.environ))

    def result_of(self, done):
        self.assertEqual(done.returncode, 0, done.stderr[-3000:])
        return json.loads(done.stdout.split("RESULT ", 1)[1])

    def assert_settled_once(self, result):
        self.assertEqual((result["state"], result["owner_started"]), ("running", True), result)
        self.assertEqual((len(self.leg_routes()), len(self.registry_rows())), (1, 1))
        record = self.record()
        self.assertEqual(record["first_leg"]["route"]["route_id"], self.leg_routes()[0].stem)
        self.assertEqual(record["first_leg"]["start_receipt"]["owner_attempt_id"], result["owner_attempt_id"])
        self.assertEqual(record["digest"], RP.decision_digest(record["decision"]))
        self.assertTrue(R.outcome_path(self.path).exists())
        outcome = json.loads(R.outcome_path(self.path).read_text(encoding="utf-8"))
        self.assertIs(outcome["terminal_gate_proven"], True)
        cycle = record["decision"]["frame_route"]["cycle_id"]
        self.assertEqual(P.read_cycle_record(self.root, cycle)["state"], "sealed")
        directory = self.jobs.parent / "completion" / self.route["route_id"]
        self.assertEqual([p.name for p in sorted(directory.glob("route-decision.[0-9]*.json"))],
                         ["route-decision.1.json"])             # the one marker of the terminal, never a second
        # One allocation from origin's tip, sealed with evidence probed there; the primary is untouched.
        leg = json.loads(self.leg_routes()[0].read_text(encoding="utf-8"))
        self.assertEqual(leg["cwd"], str((self.primary.parent / "repo-wt" / self.route["slug"]).resolve()))
        self.assertEqual({row["checked_worktree"] for row in leg["dispatch_evidence"]["tuples"]}, {leg["cwd"]})
        self.assertEqual(fixture_git(leg["cwd"], "rev-parse", "HEAD"), self.remote_sha)
        records = [json.loads(p.read_text(encoding="utf-8"))
                   for p in (self.root / ".runtime" / "worktree-preparation").glob("*.json")]
        self.assertEqual([(r["state"], str(Path(r["path"]).resolve())) for r in records], [("ready", leg["cwd"])])
        self.assertEqual((fixture_git(self.primary, "rev-parse", "HEAD"),
                          fixture_git(self.primary, "status", "--porcelain")), (self.primary_head, ""))

    def test_an_uninterrupted_run_in_a_fresh_process_settles_once_and_a_repeat_changes_nothing(self):
        first = self.result_of(self.run_driver())
        self.assert_settled_once(first)
        before = (self.record_path().read_bytes(), self.registry_rows(), [p.name for p in self.leg_routes()])
        second = self.result_of(self.run_driver())
        self.assertEqual({k: v for k, v in second.items() if k != "route_decision"},
                         {k: v for k, v in first.items() if k != "route_decision"})
        self.assertEqual((self.record_path().read_bytes(), self.registry_rows(), [p.name for p in self.leg_routes()]), before)

    def test_a164_6_every_boundary_resumes_to_one_record_one_route_and_one_attempt(self):
        for boundary in BOUNDARIES:
            with self.subTest(boundary):
                self.tearDown()
                self.setUp()
                crashed = self.run_driver(boundary)
                self.assertNotEqual(crashed.returncode, 0, (boundary, crashed.stdout[-500:]))
                if boundary != "after-manifest":
                    self.assertEqual(crashed.returncode, CRASH_EXIT, (boundary, crashed.stderr[-2000:]))
                saved = self.record_path().read_bytes() if self.record_path().exists() else None
                rows_then = len(self.registry_rows())
                self.assertLessEqual(rows_then, 1)
                result = self.result_of(self.run_driver())
                self.assert_settled_once(result)
                if saved is not None:
                    self.assertEqual(json.loads(saved)["decision"], self.record()["decision"])
                    self.assertEqual(json.loads(saved)["digest"], self.record()["digest"])
                again = self.result_of(self.run_driver())
                self.assertEqual({k: v for k, v in again.items() if k != "route_decision"},
                                 {k: v for k, v in result.items() if k != "route_decision"})
                self.assertEqual((len(self.leg_routes()), len(self.registry_rows())), (1, 1))

    def test_the_decision_is_written_before_anything_else_and_a_crash_before_it_writes_none(self):
        crashed = self.run_driver("after-intent-save")
        self.assertEqual(crashed.returncode, CRASH_EXIT)
        self.assertFalse(self.record_path().exists())
        self.assertEqual((self.leg_routes(), self.registry_rows()), ([], []))
        crashed = self.run_driver("after-decision-write")
        self.assertEqual(crashed.returncode, CRASH_EXIT)
        record = self.record()
        self.assertNotIn("first_leg", record)
        self.assertEqual((self.leg_routes(), self.registry_rows()), ([], []))
        self.assertFalse(R.outcome_path(self.path).exists())

    def test_a_crash_between_publishing_the_route_and_binding_its_id_does_not_make_a_second_route(self):
        self.assertEqual(self.run_driver("after-route-publish").returncode, CRASH_EXIT)
        self.assertEqual(len(self.leg_routes()), 1)
        self.assertNotIn("route", self.record()["first_leg"])
        published = self.leg_routes()[0].read_bytes()
        self.assert_settled_once(self.result_of(self.run_driver()))
        self.assertEqual(self.leg_routes()[0].read_bytes(), published)           # never rewritten or recompiled

    def test_a_crash_after_the_row_was_written_but_before_the_receipt_reuses_that_attempt(self):
        self.assertEqual(self.run_driver("run-after-row").returncode, CRASH_EXIT)
        self.assertEqual(len(self.registry_rows()), 1)
        self.assertNotIn("start_receipt", self.record()["first_leg"])
        before = self.registry_rows()
        self.assert_settled_once(self.result_of(self.run_driver()))
        self.assertEqual(self.registry_rows(), before)



if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--driver":
        raise SystemExit(driver(sys.argv[2], sys.argv[3]))
    unittest.main()
