#!/usr/bin/env python3
"""Tests for `artifact_workflow_group_review.py`: target selection, response validation,
merge apply, judgement record, dry-run, seal trigger, and isolation.

Real activate/begin/close/finalize fixtures come from `artifact_producer.test.py`. The
model is never called: every test injects `invoke` or mocks the provider cascade.
"""
import hashlib
import importlib.util
import io
import json
import os
import re
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import artifact_admission as adm  # noqa: E402
import artifact_history as H  # noqa: E402
import artifact_meta as M  # noqa: E402
import artifact_producer as P  # noqa: E402
import artifact_workflow_group_review as R  # noqa: E402
import artifact_workflow_groups as W  # noqa: E402
import campaign_title_repair as repair  # noqa: E402

_SPEC = importlib.util.spec_from_file_location(
    "workflow_group_review_producer_fixture", Path(__file__).with_name("artifact_producer.test.py"))
fixture = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(fixture)

PAST = 1_700_000_000.0
START = "=== CAMPAIGN DATA ===\n"
END = "\n=== END DATA ==="


def data_of(prompt):
    return json.loads(prompt.split(START, 1)[1].rsplit(END, 1)[0])


def target_ids(prompt):
    return [row["cycle_id"] for row in data_of(prompt)["targets"]]


def group_ids(prompt):
    return data_of(prompt)["group_target_ids"]


def meta_for(prompt, title="쉬운 제목", summary="한 일과 결과를 한 줄로 적음"):
    """A valid v2 `metadata` + `new_branches` pair for whatever the prompt asks (the model is never called)."""
    data = data_of(prompt)
    vocab = [item["code"] for item in data["project_meta"]["branches"]]
    entity = {"title": title, "summary": summary, "branches": [vocab[0] if vocab else "GEN"], "kinds": ["평가"]}
    return {"metadata": {"campaign": dict(entity), "cycles": {cid: dict(entity) for cid in data["metadata_target_ids"]}},
            "new_branches": [] if vocab else [{"code": "GEN", "label": "일반", "note": "기본 갈래"}]}


def _no_duplicates(pairs):
    keys = [key for key, _ in pairs]
    if len(set(keys)) != len(keys):
        raise ValueError("duplicate key")
    return dict(pairs)


def _complete(text, prompt):
    """`text` with the first JSON object given the v2 `metadata` keys it lacks; anything else is left alone."""
    start = text.find("{")
    try:
        value, end = json.JSONDecoder(object_pairs_hook=_no_duplicates).raw_decode(text, start)
    except ValueError:
        return text
    if not isinstance(value, dict) or "metadata" in value or "decisions" not in value:
        return text
    return text[:start] + json.dumps({**value, **meta_for(prompt)}, ensure_ascii=False) + text[end:]


def answer(payload):
    """An `invoke` answering `payload`; a legacy three-key answer is completed with metadata from the prompt."""
    def invoke(prompt):
        text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
        return _complete(text, prompt), "claude"
    return invoke


def none_for(prompt, reason="No shared subgoal is visible in the bodies."):
    return json.dumps({"decisions": [{"cycle_id": cid, "verdict": "none", "reason": reason}
                                     for cid in group_ids(prompt)], "new_groups": [], "relations": [],
                       **meta_for(prompt)}, ensure_ascii=False)


class Recorder:
    """An `invoke` that records each prompt and answers `none` for its targets."""

    def __init__(self, reply=None):
        self.prompts = []
        self.reply = reply or none_for

    def __call__(self, prompt):
        self.prompts.append(prompt)
        return self.reply(prompt), "claude"

    @property
    def targets(self):
        return [target_ids(prompt) for prompt in self.prompts]


def tree_snapshot(root):
    rows = []
    for path in sorted(Path(root).rglob("*")):
        meta = path.lstat()
        digest = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() and not path.is_symlink() else ""
        rows.append((str(path.relative_to(root)), meta.st_size, meta.st_mtime_ns, digest))
    return rows


class ReviewBase(fixture.ProducerTestBase):
    def setUp(self):
        self._attempt = os.environ.pop("AGENT_DISPATCH_ATTEMPT_ID", None)
        self.addCleanup(self._restore_attempt)
        super().setUp()
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop(R.DISABLE_ENV, None)
        os.environ.pop(M.TITLE_DISABLE_ENV, None)  # the runner switches both jobs off; these suites turn them on
        self.activate()

    def _restore_attempt(self):
        if self._attempt is not None:
            os.environ["AGENT_DISPATCH_ATTEMPT_ID"] = self._attempt

    # -- fixtures -------------------------------------------------------
    def start(self, key="camp", slug="cycle", now=None):
        route, route_file = self.route(slug=slug, campaign_key=key)
        begun = P.begin(self.root, route_file=route_file, capability="autopilot-code",
                        intensity="direct", campaign_key=key, now=now)
        return route, route_file, begun

    def finish(self, route, route_file, begun, body=b"# Body\n\nwork\n", now=None):
        self.write_output(begun, "reports/final_report.md", body)
        self.close(route, route_file)
        P.finalize(self.root, cycle_id=begun["cycle_id"], primary="reports/final_report.md", now=now)

    def seal(self, key="camp", slug="cycle", body=b"# Body\n\nwork\n", now=None):
        route, route_file, begun = self.start(key, slug, now)
        self.finish(route, route_file, begun, body, now)
        return begun

    def ready(self, key="camp", slug="cycle"):
        """A closed route with output written; `P.finalize` is left to the caller."""
        route, route_file, begun = self.start(key, slug)
        self.write_output(begun, "reports/final_report.md", b"# Body\n\nwork\n")
        self.close(route, route_file)
        return begun

    def finalize(self, begun):
        P.finalize(self.root, cycle_id=begun["cycle_id"], primary="reports/final_report.md")
        return begun

    def path_of(self, begun):
        directory = P.cycle_dir(self.root, begun["campaign_id"], begun["cycle_id"])
        manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
        rel = manifest["artifact_revisions"][0]["locator"]["path"]
        return (directory / rel).relative_to(self.root).as_posix()

    def declare(self, campaign_id, groups):
        plan = W.prepare(self.root, campaign_id, {"groups": groups})
        W.apply(self.root, plan)
        return plan["document"]["groups"]

    def declaration(self, campaign_id):
        return W._load_existing(W.declaration_path(self.root, campaign_id))

    def record(self):
        status, doc = R.read_record(self.root)
        self.assertEqual(status, "ok")
        return doc

    @staticmethod
    def report(result, index=0):
        return result["campaigns"][index]


class SelectionTest(ReviewBase):
    def test_enrollment_bounds_the_automatic_backlog(self):  # T1a
        old = self.seal(slug="old", now=PAST)
        trigger = self.seal(slug="trigger")
        first = Recorder()
        result = R.sweep(self.root, auto=True, cycles=[trigger["cycle_id"]], invoke=first)
        self.assertEqual(first.targets, [[trigger["cycle_id"]]])
        self.assertEqual(result["status"], "ok")
        self.assertTrue(self.record()["enrolled_at"])
        fresh = self.seal(slug="fresh")
        second = Recorder()
        R.sweep(self.root, auto=True, invoke=second)
        self.assertEqual(second.targets, [[fresh["cycle_id"]]])
        explicit = Recorder()
        R.sweep(self.root, cycles=[old["cycle_id"]], invoke=explicit)
        self.assertEqual(explicit.targets, [[old["cycle_id"]]])
        since = Recorder()
        third = self.seal(slug="third", now=PAST + 5)
        R.sweep(self.root, since=P._rfc3339(PAST + 1), invoke=since,
                dry_run=True)
        self.assertIn(third["cycle_id"], sum(since.targets, []))

    def test_no_enrollment_means_only_trigger_cycles(self):  # T1b
        for setup in ("no-file", "no-enrolled-at"):
            with self.subTest(setup):
                old = [self.seal(key=f"c-{setup}", slug=f"old{i}", now=PAST + i) for i in range(3)]
                trigger = self.seal(key=f"c-{setup}", slug="trigger")
                R.record_path(self.root).unlink(missing_ok=True)
                if setup == "no-enrolled-at":
                    self.assertTrue(R._update_record(self.root, lambda doc: None))
                    self.assertIsNone(self.record()["enrolled_at"])
                invoke = Recorder()
                with mock.patch.object(R, "ensure_enrolled", return_value=False):
                    R.sweep(self.root, auto=True, cycles=[trigger["cycle_id"]], invoke=invoke)
                self.assertEqual(invoke.targets, [[trigger["cycle_id"]]])
                self.assertFalse({o["cycle_id"] for o in old} & set(sum(invoke.targets, [])))
                R.record_path(self.root).unlink(missing_ok=True)

    def test_enrollment_survives_a_failed_review(self):  # T1c
        for reply in ("", "not json"):
            with self.subTest(reply=reply):
                R.record_path(self.root).unlink(missing_ok=True)
                cycle = self.seal(slug=f"c{len(reply)}")
                R.sweep(self.root, auto=True, cycles=[cycle["cycle_id"]], invoke=lambda prompt, r=reply: (r, None))
                self.assertTrue(self.record()["enrolled_at"])

    def test_failed_enrollment_still_limits_to_trigger(self):  # T1d
        for index in range(2):
            self.seal(slug=f"old{index}", now=PAST + index)
        trigger = self.seal(slug="trigger")
        invoke = Recorder()
        with mock.patch.object(adm, "_acquire_lock", side_effect=adm.AdmissionBusy("busy")):
            result = R.sweep(self.root, auto=True, cycles=[trigger["cycle_id"]], invoke=invoke)
        self.assertEqual(invoke.targets, [[trigger["cycle_id"]]])
        self.assertEqual(self.report(result)["record"], "unwritable")
        self.assertEqual(R.read_record(self.root)[0], "missing")

    def test_unreadable_or_foreign_record_stops_auto_and_keeps_pending(self):  # T1e
        trigger = self.seal(slug="trigger")
        valid = R._new_record(self.root)
        foreign = dict(valid, artifact_root_id="root_" + "e" * 32)
        for label, content in (("corrupt", b"{not json"), ("foreign", json.dumps(foreign).encode())):
            with self.subTest(label):
                path = R.record_path(self.root)
                path.write_bytes(content)
                invoke = Recorder()
                with redirect_stderr(io.StringIO()):
                    result = R.sweep(self.root, auto=True, cycles=[trigger["cycle_id"]], invoke=invoke)
                self.assertEqual(invoke.prompts, [])
                self.assertEqual(result["record"], "unwritable")
                self.assertEqual(path.read_bytes(), content)
                self.assertTrue((R.pending_dir(self.root) / trigger["cycle_id"]).exists())
                # An explicit run still judges, but leaves the bad file alone.
                explicit = Recorder()
                result = R.sweep(self.root, cycles=[trigger["cycle_id"]], invoke=explicit)
                self.assertEqual(explicit.targets, [[trigger["cycle_id"]]])
                self.assertEqual(self.report(result)["record"], "unwritable")
                self.assertEqual(path.read_bytes(), content)
                path.unlink()
                (R.pending_dir(self.root) / trigger["cycle_id"]).unlink()

    def test_existing_members_are_never_sent(self):  # T2
        a, b, c = (self.seal(slug=name) for name in ("a", "b", "c"))
        self.declare(a["campaign_id"], [{"title": "Existing", "members": [
            {"cycle_id": a["cycle_id"], "stage_label": "One"},
            {"cycle_id": b["cycle_id"], "stage_label": "Two"}], "relations": []}])
        invoke = Recorder()
        result = R.sweep(self.root, cycles=[a["cycle_id"], c["cycle_id"]], invoke=invoke)
        # A member is sent only for metadata: it is a target but gets no group decision.
        self.assertEqual([sorted(ids) for ids in invoke.targets], [sorted([a["cycle_id"], c["cycle_id"]])])
        self.assertEqual(group_ids(invoke.prompts[0]), [c["cycle_id"]])
        self.assertEqual(result["already_member"], [])
        self.assertEqual(self.record()["cycles"][a["cycle_id"]]["verdict"], "member")
        again = Recorder()
        result = R.sweep(self.root, cycles=[a["cycle_id"]], invoke=again)  # it has metadata now
        self.assertEqual(again.prompts, [])
        self.assertEqual(result["already_member"], [a["cycle_id"]])
        auto = Recorder()
        R.sweep(self.root, auto=True, cycles=[b["cycle_id"]], invoke=auto)
        self.assertEqual((auto.targets, group_ids(auto.prompts[0])), ([[b["cycle_id"]]], []))
        auto = Recorder()
        R.sweep(self.root, auto=True, cycles=[b["cycle_id"]], invoke=auto)
        self.assertEqual(auto.prompts, [])  # judged once: its record says `member`

    def test_retry_limit_and_reassessment_after_seal(self):  # T3
        cycle = self.seal(slug="retry")
        cid = cycle["cycle_id"]
        R.sweep(self.root, auto=True, cycles=[cid], invoke=answer(""))
        entry = self.record()["cycles"][cid]
        self.assertEqual((entry["verdict"], entry["failure_class"], entry["failures"], entry["hard_failures"]),
                         ("failed", "unavailable", 1, 0))
        R.sweep(self.root, auto=True, invoke=answer(""))
        self.assertEqual(self.record()["cycles"][cid]["hard_failures"], 0)
        self.assertEqual(self.record()["cycles"][cid]["failures"], 2)
        for expected in (1, 2, 3):
            invoke = Recorder(lambda prompt: "garbage")
            R.sweep(self.root, auto=True, invoke=invoke)
            self.assertEqual(len(invoke.prompts), 1)
            self.assertEqual(self.record()["cycles"][cid]["hard_failures"], expected)
        limited = Recorder()
        R.sweep(self.root, auto=True, invoke=limited)
        self.assertEqual(limited.prompts, [])
        explicit = Recorder()
        R.sweep(self.root, cycles=[cid], invoke=explicit)
        self.assertEqual(explicit.targets, [[cid]])
        self.assertEqual(self.record()["cycles"][cid]["verdict"], "unassigned")

    def test_open_unassigned_is_reassessed_once_after_sealing(self):  # T3
        seed = self.seal(slug="seed")
        R.sweep(self.root, auto=True, cycles=[seed["cycle_id"]], invoke=Recorder())
        route, route_file, begun = self.start(slug="opened")
        cid = begun["cycle_id"]
        R.sweep(self.root, cycles=[cid], include_open=True, invoke=Recorder())
        entry = self.record()["cycles"][cid]
        self.assertEqual((entry["verdict"], entry["cycle_state"]), ("unassigned", "open"))
        self.finish(route, route_file, begun)
        again = Recorder()
        R.sweep(self.root, auto=True, invoke=again)
        self.assertIn(cid, sum(again.targets, []))
        entry = self.record()["cycles"][cid]
        self.assertEqual((entry["verdict"], entry["cycle_state"]), ("unassigned", "sealed"))
        for _ in range(2):
            later = Recorder()
            R.sweep(self.root, auto=True, invoke=later)
            self.assertNotIn(cid, sum(later.targets, []))

    def test_withdrawn_empty_is_recorded_without_a_model_and_never_reassessed(self):
        cycle = self.seal(slug="withdrawn")
        cid = cycle["cycle_id"]
        item = R.Outcome(cycle_id=cid, campaign_id=cycle["campaign_id"], verdict="withdrawn-empty",
                         group_id="wgrp_" + "a" * 32, reason="cycle ended with no durable output",
                         cycle_state="abandoned", declaration_sha256="sha256:" + "b" * 64, profile=None)
        self.assertTrue(R.record_outcomes(self.root, [item], mode="autoclose", now=None, lock_timeout=0))
        entry = self.record()["cycles"][cid]
        self.assertEqual((entry["verdict"], entry["mode"], entry["profile"], entry["cycle_state"]),
                         ("withdrawn-empty", "autoclose", None, "abandoned"))
        self.assertEqual(entry["group_id"], "wgrp_" + "a" * 32)
        _status, doc = R.read_record(self.root)
        picked = R.select_targets(self.root, doc, auto=True, cycles=[cid])
        self.assertEqual(picked.by_campaign, {})
        self.assertIn(cid, picked.considered)
        # A held admission lock is a soft failure, not a wait.
        lock = R.admission._acquire_lock(self.root, 5)
        try:
            self.assertFalse(R.record_outcomes(self.root, [item], mode="autoclose", now=None, lock_timeout=0))
        finally:
            R.admission._release_lock(self.root, lock)

    def test_limit_applies_before_the_campaign_split(self):
        ids = [self.seal(key=key, slug=f"{key}{i}", now=PAST + n)["cycle_id"]
               for n, (key, i) in enumerate([("one", 0), ("two", 0), ("one", 1)])]
        invoke = Recorder()
        R.sweep(self.root, cycles=ids, limit=2, invoke=invoke)
        self.assertEqual(sorted(sum(invoke.targets, [])), sorted(ids[:2]))
        self.assertEqual(len(invoke.prompts), 2)


class ValidationTest(ReviewBase):
    def setUp(self):
        super().setUp()
        self.a1 = self.seal(slug="a1")
        self.a2 = self.seal(slug="a2")
        self.context = self.seal(slug="context")
        self.target = self.seal(slug="target")
        self.other = self.seal(key="other", slug="elsewhere")
        self.camp = self.a1["campaign_id"]
        self.groups = self.declare(self.camp, [{"title": "Existing", "members": [
            {"cycle_id": self.a1["cycle_id"], "stage_label": "One"},
            {"cycle_id": self.a2["cycle_id"], "stage_label": "Two"}], "relations": []}])
        self.gid = self.groups[0]["group_id"]
        self.declared = W.declaration_path(self.root, self.camp).read_bytes()
        self.tid = self.target["cycle_id"]

    def run_reply(self, reply):
        return R.sweep(self.root, cycles=[self.tid], invoke=answer(reply))

    def decision(self, **extra):
        return {"cycle_id": self.tid, "verdict": "none", "reason": "No shared subgoal.", **extra}

    def new_group(self, members=None, title="Fresh workflow"):
        return {"key": "g1", "title": title, "members": members or [
            {"cycle_id": self.tid, "stage_label": "Start"},
            {"cycle_id": self.context["cycle_id"], "stage_label": "Earlier"}]}

    def new_reply(self, **group):
        return {"decisions": [{"cycle_id": self.tid, "verdict": "new", "new_group": "g1",
                               "stage_label": "Start", "reason": "Same subgoal."}],
                "new_groups": [self.new_group(**group)], "relations": []}

    def test_invalid_responses_reject_the_campaign_and_keep_the_declaration(self):  # T4
        bad_member = [{"cycle_id": self.tid, "stage_label": "Start"},
                      {"cycle_id": self.other["cycle_id"], "stage_label": "Elsewhere"}]
        duplicate = ('{"decisions":[{"cycle_id":"%s","verdict":"none","verdict":"none","reason":"r"}],'
                     '"new_groups":[],"relations":[]}' % self.tid)
        cases = {
            "unknown-group": {"decisions": [{"cycle_id": self.tid, "verdict": "join", "group_id": "wgrp_" + "0" * 32,
                                             "stage_label": "S", "reason": "r"}], "new_groups": [], "relations": []},
            "non-target": {"decisions": [self.decision(), self.decision(cycle_id=self.a1["cycle_id"])],
                           "new_groups": [], "relations": []},
            "missing": {"decisions": [], "new_groups": [], "relations": []},
            "other-campaign": self.new_reply(members=bad_member),
            "long-title": self.new_reply(title="t" * 121),
            "control-char": {"decisions": [self.decision(reason="bad\x01reason")], "new_groups": [], "relations": []},
            "duplicate-key": duplicate,
            "not-json": "here you go: {}",
            "unreferenced": {"decisions": [self.decision()], "new_groups": [self.new_group()], "relations": []},
            "unknown-kind": dict(self.new_reply(), relations=[{
                "from_cycle_id": self.tid, "to_cycle_id": self.context["cycle_id"], "kind": "sequel",
                "rationale": "r", "evidence_paths": [self.path_of(self.target)]}]),
        }
        for label, reply in cases.items():
            with self.subTest(label):
                report = self.report(self.run_reply(reply))
                self.assertEqual((report["status"], report["failure_class"]), ("failed", "invalid-response"))
                self.assertEqual(W.declaration_path(self.root, self.camp).read_bytes(), self.declared)
                self.assertEqual(self.record()["cycles"][self.tid]["failure_class"], "invalid-response")

    def test_fenced_reply_with_a_trailing_note_is_read(self):
        reply = {"decisions": [{"cycle_id": self.tid, "verdict": "join", "group_id": self.gid,
                                "stage_label": "Three", "reason": "Same subgoal."}],
                 "new_groups": [], "relations": []}
        text = "```json\n%s\n```\n\nNote: the join follows the shared plan." % json.dumps(reply)
        report = self.report(self.run_reply(text))
        self.assertEqual(report["status"], "applied")
        self.assertEqual(self.record()["cycles"][self.tid]["verdict"], "joined")

    def test_long_stage_label_falls_back_and_newlines_are_normalized(self):
        reply = {"decisions": [{"cycle_id": self.tid, "verdict": "join", "group_id": self.gid,
                                "stage_label": "s" * 60, "reason": "First line\nsecond   line"}],
                 "new_groups": [], "relations": []}
        report = self.report(self.run_reply(reply))
        self.assertEqual(report["status"], "applied")
        entry = self.record()["cycles"][self.tid]
        self.assertEqual(entry["stage_label"],
                         W.stage_label_from_title(P.read_cycle_record(self.root, self.tid)["title"]))
        self.assertEqual(entry["reason"], "First line second line")

    def test_bad_evidence_drops_only_the_relation(self):  # T5
        trio = [self.seal(key="rel", slug=name) for name in ("x", "y", "z")]
        x, y, z = (item["cycle_id"] for item in trio)
        px, py, pz = (self.path_of(item) for item in trio)
        good = lambda a, b, pa, pb, kind="precedes": {
            "from_cycle_id": a, "to_cycle_id": b, "kind": kind, "rationale": "Used as the input.",
            "evidence_paths": [pa, pb]}
        reply = {
            "decisions": [{"cycle_id": cid, "verdict": "new", "new_group": "g1", "stage_label": name,
                           "reason": "Same subgoal."} for cid, name in zip((x, y, z), "xyz")],
            "new_groups": [{"key": "g1", "title": "Trio", "members": [
                {"cycle_id": cid, "stage_label": name} for cid, name in zip((x, y, z), "xyz")]}],
            "relations": [good(x, y, px, py), good(y, z, py, pz), good(z, x, pz, px),
                          good(x, z, px, "campaigns/nope/artifacts/missing.md"),
                          good(x, z, px, self.path_of(self.a1))],
        }
        report = self.report(R.sweep(self.root, cycles=[x, y, z], invoke=answer(reply)))
        self.assertEqual(report["status"], "applied")
        self.assertEqual(report["accepted_relations"], 2)
        codes = {row["index"]: row["code"] for row in report["dropped_relations"]}
        self.assertEqual(codes, {2: "relation-cycle", 3: "evidence-not-candidate", 4: "evidence-not-candidate"})
        group = self.declaration(trio[0]["campaign_id"])["groups"][0]
        self.assertEqual(len(group["members"]), 3)
        self.assertEqual([(r["from_cycle_id"], r["to_cycle_id"]) for r in group["relations"]], [(x, y), (y, z)])
        self.assertEqual([ref["path"] for ref in group["relations"][0]["evidence_refs"]], [px, py])
        self.assertEqual(W.verify(self.root, trio[0]["campaign_id"])["stale_evidence"], [])

    def test_a_new_subgoal_opens_a_one_cycle_group(self):
        # 2026-09-30 SR report: a new outside request's first cycle is alone, and a
        # two-member minimum left the reviewer only join or none. A cycle group may
        # hold one cycle (user decision), so the first cycle of a new subgoal opens it.
        before = self.declaration(self.camp)["groups"]
        reply = {"decisions": [{"cycle_id": self.tid, "verdict": "new", "new_group": "g1",
                                "stage_label": "Start", "reason": "A new outside request no group covers."}],
                 "new_groups": [{"key": "g1", "title": "New request follow-up",
                                 "members": [{"cycle_id": self.tid, "stage_label": "Start"}]}],
                 "relations": []}
        self.assertEqual(self.report(self.run_reply(reply))["status"], "applied")
        groups = self.declaration(self.camp)["groups"]
        self.assertEqual(groups[:len(before)], before)
        self.assertEqual(groups[-1]["title"], "New request follow-up")
        self.assertEqual(groups[-1]["members"], [{"cycle_id": self.tid, "stage_label": "Start"}])
        self.assertEqual(W.verify(self.root, self.camp)["stale_evidence"], [])
        self.assertEqual(self.record()["cycles"][self.tid]["group_id"], groups[-1]["group_id"])

    def test_the_prompt_allows_a_one_cycle_new_group(self):
        self.assertNotIn(">= 2 members", R.PROMPT_RULES if hasattr(R, "PROMPT_RULES") else Path(R.__file__).read_text(encoding="utf-8"))

    def test_merge_keeps_existing_declaration_and_bytes(self):  # T6
        existing = self.groups[0]
        first, second = existing["members"][0]["cycle_id"], existing["members"][1]["cycle_id"]
        plan = W.prepare(self.root, self.camp, {"groups": [{"group_id": self.gid, "title": "Existing", "members": [
            {"cycle_id": first, "stage_label": "One"}, {"cycle_id": second, "stage_label": "Two"}],
            "relations": [{"from_cycle_id": first, "to_cycle_id": second, "kind": "precedes",
                           "rationale": "Two uses one.", "evidence_refs": [
                               {"path": self.path_of(self.a1)}, {"path": self.path_of(self.a2)}]}]}]})
        W.apply(self.root, plan)
        before = self.declaration(self.camp)["groups"][0]
        extra = self.seal(slug="extra")
        protected = [P.campaign_dir(self.root, self.camp) / "campaign.json",
                     R.producer.producer_dir(self.root) / "cycles" / f"{self.tid}.json"]
        protected += list((P.campaign_dir(self.root, self.camp)).rglob("manifest.json"))
        protected += list((P.campaign_dir(self.root, self.camp)).rglob(".cycle.json"))
        protected = [path for path in protected if path.exists()]
        untouched = {path: path.read_bytes() for path in protected}
        reply = {
            "decisions": [
                {"cycle_id": self.tid, "verdict": "join", "group_id": self.gid, "stage_label": "Three",
                 "reason": "Continues the same work."},
                {"cycle_id": extra["cycle_id"], "verdict": "new", "new_group": "g1", "stage_label": "Other",
                 "reason": "Separate subgoal."}],
            "new_groups": [{"key": "g1", "title": "Second workflow", "members": [
                {"cycle_id": extra["cycle_id"], "stage_label": "Other"},
                {"cycle_id": self.context["cycle_id"], "stage_label": "Prep"}]}],
            "relations": [{"from_cycle_id": second, "to_cycle_id": self.tid, "kind": "followup",
                           "rationale": "Its result was continued.",
                           "evidence_paths": [self.path_of(self.a2), self.path_of(self.target)]}]}
        result = R.sweep(self.root, cycles=[self.tid, extra["cycle_id"]], invoke=answer(reply))
        self.assertEqual(self.report(result)["status"], "applied")
        groups = self.declaration(self.camp)["groups"]
        self.assertEqual(len(groups), 2)
        self.assertEqual(groups[0]["title"], before["title"])
        self.assertEqual(groups[0]["members"][:2], before["members"])
        self.assertEqual(groups[0]["members"][2], {"cycle_id": self.tid, "stage_label": "Three"})
        self.assertEqual(groups[0]["relations"][0], before["relations"][0])
        self.assertEqual(len(groups[0]["relations"]), 2)
        self.assertRegex(groups[1]["group_id"], r"wgrp_[0-9a-f]{32}\Z")
        self.assertEqual({m["cycle_id"] for m in groups[1]["members"]}, {extra["cycle_id"], self.context["cycle_id"]})
        self.assertEqual(W.verify(self.root, self.camp)["stale_evidence"], [])
        self.assertEqual({path: path.read_bytes() for path in protected if path.name != f"{self.tid}.json"},
                         {path: untouched[path] for path in protected if path.name != f"{self.tid}.json"})
        cycles = self.record()["cycles"]
        self.assertEqual((cycles[self.tid]["verdict"], cycles[self.tid]["group_id"]), ("joined", self.gid))
        self.assertEqual(cycles[extra["cycle_id"]]["group_id"], groups[1]["group_id"])
        self.assertEqual(cycles[self.context["cycle_id"]]["verdict"], "new-group")

    def test_none_records_the_reason_and_writes_no_declaration(self):  # T7
        other = self.seal(key="lonely", slug="lonely")
        reason = "Only the title looks alike;   the bodies share nothing."
        result = R.sweep(self.root, cycles=[other["cycle_id"]], invoke=answer(
            {"decisions": [{"cycle_id": other["cycle_id"], "verdict": "none", "reason": reason}],
             "new_groups": [], "relations": []}))
        self.assertEqual(self.report(result)["status"], "applied")  # metadata is written even for `none`
        self.assertFalse(W.declaration_path(self.root, other["campaign_id"]).exists())
        self.assertEqual(M.read_campaign_meta(self.root, other["campaign_id"]).status, "ok")
        entry = self.record()["cycles"][other["cycle_id"]]
        self.assertEqual((entry["verdict"], entry["profile"], entry["harness"], entry["mode"]),
                         ("unassigned", "light", "claude", "explicit"))
        self.assertEqual(entry["reason"], "Only the title looks alike; the bodies share nothing.")
        self.assertIsNone(re.search(r"opus|sonnet|haiku|gpt|fable", R.record_path(self.root).read_text(), re.I))

    def test_dry_run_writes_nothing_under_the_artifact_root(self):  # T8
        before = tree_snapshot(self.root)
        reply = self.new_reply()
        result = R.sweep(self.root, cycles=[self.tid], dry_run=True, invoke=answer(reply))
        self.assertEqual(tree_snapshot(self.root), before)
        report = self.report(result)
        self.assertEqual(report["status"], "dry-run")
        self.assertTrue(report["after_sha256"].startswith("sha256:"))
        self.assertEqual(report["decisions"][self.tid]["verdict"], "new")
        self.assertFalse(R.record_path(self.root).exists())
        self.assertFalse(R.lock_path(self.root).exists())
        self.assertFalse(R.pending_dir(self.root).exists())

    def test_apply_conflict_is_retried_once(self):
        real = W._validated_apply_locked
        calls = []

        def racing(root, plan):
            calls.append(1)
            if len(calls) == 1:
                raise W.WorkflowGroupError("declaration-preimage-conflict")
            return real(root, plan)

        with mock.patch.object(W, "_validated_apply_locked", side_effect=racing):
            report = self.report(self.run_reply(self.new_reply()))
        self.assertEqual((report["status"], len(calls)), ("applied", 2))

    def test_apply_failure_is_a_hard_failure(self):
        with mock.patch.object(W, "_validated_apply_locked",
                               side_effect=W.WorkflowGroupError("evidence-total-size-limit")):
            report = self.report(self.run_reply(self.new_reply()))
        self.assertEqual((report["status"], report["failure_class"]), ("failed", "apply-failed"))
        self.assertEqual(self.record()["cycles"][self.tid]["hard_failures"], 1)


class TriggerTest(ReviewBase):
    def spawned(self):
        return mock.patch.object(R.subprocess, "Popen")

    def test_seal_succeeds_when_the_hook_or_spawn_fails(self):  # T9
        begun = self.ready(slug="hook-raises")
        with mock.patch.object(R, "launch_after_seal", side_effect=RuntimeError("boom")):
            self.finalize(begun)
        self.assertEqual(P.read_cycle_record(self.root, begun["cycle_id"])["state"], "sealed")
        begun = self.ready(slug="spawn-fails")
        with mock.patch.object(R, "in_test_process", return_value=False), \
                mock.patch.object(R.subprocess, "Popen", side_effect=OSError("no fork")) as popen:
            self.finalize(begun)
        self.assertTrue(popen.called)
        self.assertEqual(P.read_cycle_record(self.root, begun["cycle_id"])["state"], "sealed")
        directory = P.cycle_dir(self.root, begun["campaign_id"], begun["cycle_id"])
        self.assertTrue((directory / "manifest.json").is_file())

    def test_switch_off_disables_trigger_and_auto_sweep(self):  # T10
        cycle_env = {R.DISABLE_ENV: "off"}
        begun = self.ready(slug="switch-off")
        with mock.patch.dict(os.environ, cycle_env), mock.patch.object(R, "in_test_process", return_value=False), \
                self.spawned() as popen:
            self.finalize(begun)
            invoke = Recorder()
            self.assertEqual(R.sweep(self.root, auto=True, cycles=[begun["cycle_id"]], invoke=invoke),
                             {"status": "disabled"})
        popen.assert_not_called()
        self.assertEqual(invoke.prompts, [])
        explicit = Recorder()
        with mock.patch.dict(os.environ, cycle_env):
            R.sweep(self.root, cycles=[begun["cycle_id"]], invoke=explicit)
        self.assertEqual(explicit.targets, [[begun["cycle_id"]]])

    def test_test_process_never_spawns(self):  # T12
        begun = self.ready(slug="in-test")
        with self.spawned() as popen:
            self.finalize(begun)
        popen.assert_not_called()

    def test_busy_lock_leaves_a_pending_marker_and_the_next_sweep_takes_it(self):  # T11
        held = R._try_flock(self.root)
        self.assertIsNotNone(held)
        begun = self.ready(slug="busy")
        try:
            with mock.patch.object(R, "in_test_process", return_value=False), self.spawned() as popen, \
                    mock.patch.object(R, "select_targets") as select, mock.patch.object(R, "build_input") as build:
                self.finalize(begun)
            popen.assert_not_called()
            select.assert_not_called()
            build.assert_not_called()
            self.assertEqual(P.read_cycle_record(self.root, begun["cycle_id"])["state"], "sealed")
            self.assertTrue((R.pending_dir(self.root) / begun["cycle_id"]).exists())
            self.assertEqual(R.sweep(self.root, auto=True, cycles=["cyc_" + "1" * 32], invoke=Recorder())["status"],
                             "busy")
        finally:
            R._unlock(held)
        invoke = Recorder()
        R.sweep(self.root, auto=True, invoke=invoke)
        self.assertEqual(invoke.targets, [[begun["cycle_id"]]])
        self.assertEqual(R._pending_ids(self.root), [])

    def test_free_lock_spawns_one_detached_auto_sweep(self):  # T11
        begun = self.ready(slug="free")
        with mock.patch.object(R, "in_test_process", return_value=False), self.spawned() as popen:
            self.finalize(begun)
        popen.assert_called_once()
        argv = popen.call_args.args[0]
        self.assertEqual(argv[1], str(Path(R.__file__).resolve()))
        self.assertEqual(argv[2:4], ["sweep", "--artifact-root"])
        self.assertEqual(Path(argv[4]).resolve(), self.root.resolve())
        self.assertEqual(argv[5:], ["--auto", "--cycle", begun["cycle_id"]])
        self.assertTrue(popen.call_args.kwargs["start_new_session"])
        self.assertIsNone(popen.call_args.kwargs.get("shell"))
        self.assertFalse((R.pending_dir(self.root) / begun["cycle_id"]).exists())

    def test_pending_is_only_touched_for_a_cutover_root(self):
        with mock.patch.object(R, "in_test_process", return_value=False), self.spawned() as popen:
            self.assertFalse(R.launch_after_seal(self.root / "nowhere", {"cycle_id": "cyc_" + "2" * 32}))
            self.assertFalse(R.launch_after_seal(self.root, {"cycle_id": "not-a-cycle"}))
        popen.assert_not_called()


def content_snapshot(root):
    """rel path -> digest of every regular file except the review's own judgement record and locks."""
    skip = ("workflow-group-review", "admission")
    return {row[0]: row[3] for row in tree_snapshot(root) if row[3] and not any(word in row[0] for word in skip)}


MUTATIONS = {
    "missing-metadata": lambda v: {k: x for k, x in v.items() if k != "metadata"},
    "missing-new-branches": lambda v: {k: x for k, x in v.items() if k != "new_branches"},
    "extra-top-key": lambda v: {**v, "ids": {}},
    "cycle-missing": lambda v: {**v, "metadata": {**v["metadata"], "cycles": {}}},
    "cycle-unknown": lambda v: {**v, "metadata": {**v["metadata"], "cycles": {
        **v["metadata"]["cycles"], "cyc_" + "0" * 32: next(iter(v["metadata"]["cycles"].values()))}}},
    "source-injected": lambda v: {**v, "metadata": {**v["metadata"], "campaign": {
        **v["metadata"]["campaign"], "source": {"title": {"by": "human"}}}}},
    "id-injected": lambda v: {**v, "metadata": {**v["metadata"], "campaign": {
        **v["metadata"]["campaign"], "short_id": "CMD-01"}}},
    "title-too-long": lambda v: {**v, "metadata": {**v["metadata"], "campaign": {
        **v["metadata"]["campaign"], "title": "가" * 121}}},
    "title-empty": lambda v: {**v, "metadata": {**v["metadata"], "campaign": {**v["metadata"]["campaign"], "title": ""}}},
    "summary-too-long": lambda v: {**v, "metadata": {**v["metadata"], "campaign": {
        **v["metadata"]["campaign"], "summary": "가" * 401}}},
    "unknown-kind": lambda v: {**v, "metadata": {**v["metadata"], "campaign": {
        **v["metadata"]["campaign"], "kinds": ["없는성격"]}}},
    "unknown-branch": lambda v: {**v, "metadata": {**v["metadata"], "campaign": {
        **v["metadata"]["campaign"], "branches": ["NOPE"]}}},
    "empty-branches": lambda v: {**v, "metadata": {**v["metadata"], "campaign": {**v["metadata"]["campaign"], "branches": []}}},
    "duplicate-branch": lambda v: {**v, "metadata": {**v["metadata"], "campaign": {
        **v["metadata"]["campaign"], "branches": ["GEN", "GEN"]}}},
    "bad-new-branch-code": lambda v: {**v, "new_branches": [{"code": "gen", "label": "x", "note": ""}]},
    "etc-as-new-branch": lambda v: {**v, "new_branches": [{"code": "ETC", "label": "x", "note": ""}]},
    "duplicate-new-branch": lambda v: {**v, "new_branches": [v["new_branches"][0], v["new_branches"][0]]},
    "too-many-new-branches": lambda v: {**v, "new_branches": [
        {"code": f"B{chr(65 + i)}", "label": "x", "note": ""} for i in range(13)]},
    "new-branch-extra-key": lambda v: {**v, "new_branches": [{**v["new_branches"][0], "extra": 1}]},
}


class UnifiedReviewTest(ReviewBase):
    """One model call per campaign decides the groups and the metadata; failures write nothing."""

    def setUp(self):
        super().setUp()
        self.a1 = self.seal(slug="a1")
        self.a2 = self.seal(slug="a2")
        self.b1 = self.seal(key="other", slug="b1")
        self.camp, self.other = self.a1["campaign_id"], self.b1["campaign_id"]

    def meta_doc(self, begun):
        read = M.read_campaign_meta(self.root, begun["campaign_id"])
        self.assertEqual(read.status, "ok", read.code)
        return read.doc

    def valid_reply(self, ids):
        prompt_data = {"project_meta": {"branches": []}, "metadata_target_ids": ids}
        entity = {"title": "쉬운 제목", "summary": "한 일과 결과", "branches": ["GEN"], "kinds": ["평가"]}
        return {"decisions": [{"cycle_id": cid, "verdict": "none", "reason": "r"} for cid in ids],
                "new_groups": [], "relations": [],
                "metadata": {"campaign": entity, "cycles": {cid: dict(entity) for cid in prompt_data["metadata_target_ids"]}},
                "new_branches": [{"code": "GEN", "label": "일반", "note": ""}]}

    def test_one_call_per_campaign_decides_groups_and_metadata_together(self):
        invoke = Recorder()
        result = R.sweep(self.root, cycles=[self.a1["cycle_id"], self.a2["cycle_id"], self.b1["cycle_id"]], invoke=invoke)
        self.assertEqual(len(invoke.prompts), 2)  # one per campaign, whatever the target count
        self.assertEqual(sorted(len(t) for t in invoke.targets), [1, 2])
        self.assertEqual({r["status"] for r in result["campaigns"]}, {"applied"})
        for begun in (self.a1, self.b1):
            doc = self.meta_doc(begun)
            self.assertEqual(doc["campaign"]["title"], "쉬운 제목")
            self.assertTrue(all(e["short_id"].startswith("GEN-0") for e in doc["cycles"].values()))
        self.assertEqual(sorted(self.meta_doc(c)["campaign"]["short_id"] for c in (self.a1, self.b1)),
                         ["GEN-01", "GEN-02"])  # seals in one second tie, so the order between campaigns is not fixed
        self.assertEqual([b["code"] for b in M.read_project(self.root).doc["branches"]], ["GEN"])

    def test_the_prompt_carries_vocabulary_protected_fields_and_the_two_target_lists(self):
        invoke = Recorder()
        R.sweep(self.root, cycles=[self.a1["cycle_id"]], invoke=invoke)
        data = data_of(invoke.prompts[0])
        self.assertEqual(data["group_target_ids"], [self.a1["cycle_id"]])
        self.assertEqual(data["metadata_target_ids"], [self.a1["cycle_id"]])
        self.assertEqual(set(data["project_meta"]), {"display_name", "branches", "kinds", "general_branch_limit",
                                                     "new_branch_allowance"})
        self.assertEqual((data["project_meta"]["kinds"], data["project_meta"]["new_branch_allowance"]),
                         (list(M.KINDS), 12))
        self.assertEqual(data["protected_fields"], {"campaign": [], "cycles": {self.a1["cycle_id"]: []}})
        self.assertIn("own new group", invoke.prompts[0])  # the one-member rule is in the prompt
        # a person's fields are named as protected in the next call
        M.run_write(self.root, lambda ws: M.op_set(ws, self.camp, None, {"summary": "사람 요약"}), now=1_790_000_000.0)
        again = Recorder()
        R.sweep(self.root, cycles=[self.a2["cycle_id"]], invoke=again)
        self.assertEqual(data_of(again.prompts[0])["protected_fields"]["campaign"], ["summary"])
        self.assertEqual(data_of(again.prompts[0])["campaign_meta"]["summary"], "사람 요약")
        self.assertEqual(data_of(again.prompts[0])["project_meta"]["new_branch_allowance"], 1)

    def test_a_none_decision_still_writes_metadata_and_a_group_member_context_is_untouched(self):
        context = self.seal(slug="context")
        self.declare(self.camp, [{"title": "Existing", "members": [
            {"cycle_id": self.a1["cycle_id"], "stage_label": "One"}], "relations": []}])
        declared = W.declaration_path(self.root, self.camp).read_bytes()
        invoke = Recorder()
        result = R.sweep(self.root, cycles=[self.a2["cycle_id"], self.a1["cycle_id"]], invoke=invoke)
        self.assertEqual(group_ids(invoke.prompts[0]), [self.a2["cycle_id"]])
        self.assertEqual(sorted(target_ids(invoke.prompts[0])), sorted([self.a1["cycle_id"], self.a2["cycle_id"]]))
        self.assertEqual(self.report(result)["status"], "applied")
        self.assertEqual(W.declaration_path(self.root, self.camp).read_bytes(), declared)  # `none` leaves the groups
        cycles = self.meta_doc(self.a1)["cycles"]
        self.assertEqual(set(cycles), {self.a1["cycle_id"], self.a2["cycle_id"]})  # not the ungrouped context cycle
        self.assertNotIn(context["cycle_id"], cycles)

    def test_the_tail_beyond_the_per_call_cap_is_not_sent_twice_and_waits_for_the_next_sweep(self):
        extra = self.seal(slug="a3")
        ids = [self.a1["cycle_id"], self.a2["cycle_id"], extra["cycle_id"]]
        invoke = Recorder()
        with mock.patch.object(R, "MAX_TARGETS_PER_CALL", 2):
            result = R.sweep(self.root, cycles=ids, invoke=invoke)
        self.assertEqual(len(invoke.prompts), 1)
        (tail,) = self.report(result)["deferred"]  # which one waits depends on seal order; seals in one second tie
        self.assertIn(tail, ids)
        self.assertEqual(sorted(invoke.targets[0] + [tail]), sorted(ids))
        self.assertNotIn(tail, self.record()["cycles"])  # not marked handled
        self.assertNotIn(tail, self.meta_doc(self.a1)["cycles"])
        second = Recorder()
        R.sweep(self.root, cycles=[tail], invoke=second)
        self.assertEqual(second.targets, [[tail]])
        self.assertIn(tail, self.meta_doc(self.a1)["cycles"])
        auto = Recorder()  # an automatic sweep keeps the same rule: the campaign is asked once per sweep
        sealed = [self.seal(slug=f"n{i}") for i in range(3)]
        with mock.patch.object(R, "MAX_TARGETS_PER_CALL", 2):
            R.sweep(self.root, auto=True, cycles=[s["cycle_id"] for s in sealed], invoke=auto)
        self.assertEqual(len([p for p in auto.prompts if data_of(p)["campaign"]["campaign_id"] == self.camp]), 1)

    def test_a_cycle_sealed_during_the_call_waits_for_the_next_sweep_instead_of_a_second_call(self):
        late, asked = [], []

        def invoke(prompt):
            asked.append(prompt)
            if not late:  # a seal of the same campaign lands while the model is answering
                begun = self.seal(slug="late")
                R._touch_pending(self.root, begun["cycle_id"])
                late.append(begun["cycle_id"])
            return none_for(prompt), "claude"

        first = R.sweep(self.root, auto=True, cycles=[self.a1["cycle_id"]], campaign_ids=[self.camp], invoke=invoke)
        self.assertEqual(len(asked), 1)  # one call for the campaign in the whole sweep
        self.assertEqual(len(first["campaigns"]), 1)
        self.assertEqual(R._pending_ids(self.root), late)  # kept, not dropped
        self.assertNotIn(late[0], self.record()["cycles"])  # and not marked handled
        second = Recorder()
        R.sweep(self.root, auto=True, campaign_ids=[self.camp], invoke=second)
        self.assertEqual(second.targets, [[late[0]]])
        self.assertEqual(R._pending_ids(self.root), [])
        self.assertIn(late[0], self.meta_doc(self.a1)["cycles"])

    def test_every_malformed_answer_rejects_the_campaign_and_writes_nothing(self):
        before = None
        for label, mutate in MUTATIONS.items():
            with self.subTest(label):
                def reply(prompt, mutate=mutate):
                    data = data_of(prompt)
                    ids = data["metadata_target_ids"]
                    base = json.loads(none_for(prompt))
                    base["new_branches"] = base["new_branches"] or [{"code": "GEN", "label": "일반", "note": ""}]
                    return json.dumps(mutate(base), ensure_ascii=False)
                before = content_snapshot(self.root)
                report = self.report(R.sweep(self.root, cycles=[self.a1["cycle_id"]], invoke=Recorder(reply)))
                self.assertEqual((report["status"], report["failure_class"]), ("failed", "invalid-response"), report)
                self.assertEqual(content_snapshot(self.root), before)
                self.assertEqual(M.read_campaign_meta(self.root, self.camp).status, "missing")
                self.assertFalse((self.root / M.PROJECT_REL).exists())
                self.assertFalse((self.root / M.STATE_REL).exists())
                self.assertFalse((self.root / H.HISTORY_REL).exists())
        self.assertEqual(self.record()["cycles"][self.a1["cycle_id"]]["failure_class"], "invalid-response")

    def test_a_decision_for_a_cycle_that_already_has_a_group_is_rejected(self):
        self.declare(self.camp, [{"title": "Existing", "members": [
            {"cycle_id": self.a1["cycle_id"], "stage_label": "One"}], "relations": []}])

        def reply(prompt):
            base = json.loads(none_for(prompt))
            base["decisions"].append({"cycle_id": self.a1["cycle_id"], "verdict": "none", "reason": "r"})
            return json.dumps(base, ensure_ascii=False)

        before = content_snapshot(self.root)
        report = self.report(R.sweep(self.root, cycles=[self.a1["cycle_id"], self.a2["cycle_id"]], invoke=Recorder(reply)))
        self.assertEqual((report["status"], report["failure_class"]), ("failed", "invalid-response"))
        self.assertEqual(content_snapshot(self.root), before)

    def test_model_unavailable_conflict_and_a_broken_meta_leave_every_judged_file_alone(self):
        before = content_snapshot(self.root)
        for label, invoke in (("empty", lambda prompt: ("", None)), ("not-json", lambda prompt: ("nope", "claude")),
                              ("raises-as-empty", lambda prompt: ("   ", "claude"))):
            with self.subTest(label):
                report = self.report(R.sweep(self.root, cycles=[self.a1["cycle_id"]], invoke=invoke))
                self.assertEqual(report["status"], "failed")
                self.assertEqual(content_snapshot(self.root), before)
        def new_group(prompt):
            base = json.loads(none_for(prompt))
            cid = self.a1["cycle_id"]
            base["decisions"] = [{"cycle_id": cid, "verdict": "new", "new_group": "g1", "stage_label": "S", "reason": "r"}]
            base["new_groups"] = [{"key": "g1", "title": "Fresh", "members": [{"cycle_id": cid, "stage_label": "S"}]}]
            return json.dumps(base, ensure_ascii=False)

        with mock.patch.object(W, "_validated_apply_locked",
                               side_effect=W.WorkflowGroupError("declaration-preimage-conflict")):
            report = self.report(R.sweep(self.root, cycles=[self.a1["cycle_id"]], invoke=Recorder(new_group)))
        self.assertEqual((report["status"], report["failure_class"]), ("failed", "apply-failed"))
        self.assertEqual(content_snapshot(self.root), before)
        # a meta.json that breaks the contract: the campaign is skipped before any model call
        meta_path = self.root / M._campaign_location(self.root, self.camp)[1]
        meta_path.write_text("{broken", encoding="utf-8")
        broken = content_snapshot(self.root)
        calls = Recorder()
        with redirect_stderr(io.StringIO()) as err:
            result = R.sweep(self.root, cycles=[self.a1["cycle_id"]], invoke=calls)
        self.assertEqual(calls.prompts, [])
        self.assertTrue(result["skipped"][0]["reason"].startswith("meta-invalid"))
        self.assertIn("campaign skipped", err.getvalue())
        self.assertEqual(content_snapshot(self.root), broken)
        # the other campaign is not held up by it
        other = Recorder()
        R.sweep(self.root, cycles=[self.b1["cycle_id"]], invoke=other)
        self.assertEqual(len(other.prompts), 1)

    def test_a_person_editing_during_the_model_call_wins(self):
        def invoke(prompt):
            M.run_write(self.root, lambda ws: M.op_branch_add(ws, "GEN", "일반", ""), now=1_790_000_000.0)
            M.run_write(self.root, lambda ws: M.op_set(ws, self.camp, None, {"title": "그 사이 사람이 정함"}),
                        now=1_790_000_000.0)
            return none_for(prompt), "claude"

        R.sweep(self.root, cycles=[self.a1["cycle_id"]], invoke=invoke)
        doc = self.meta_doc(self.a1)
        self.assertEqual((doc["campaign"]["title"], doc["campaign"]["source"]["title"]["by"]),
                         ("그 사이 사람이 정함", "human"))
        self.assertEqual(doc["campaign"]["summary"], "한 일과 결과를 한 줄로 적음")
        self.assertEqual(doc["cycles"][self.a1["cycle_id"]]["title"], "쉬운 제목")

    def test_an_old_declaration_title_is_kept_and_the_declaration_is_unchanged(self):
        path = self.root / repair.DISPLAY_TITLE_REL
        path.write_text(json.dumps({"schema": repair.DECLARATION_SCHEMA, "artifact_root_id": fixture.ROOT_ID, "entries": [{
            "campaign_id": self.camp, "campaign_locator": "x", "display_title": "사람이 선언한 제목",
            "manifest_bindings": [], "manifest_revision_ids": [], "manifest_digests": []}]}, ensure_ascii=False),
            encoding="utf-8")
        before = path.read_bytes()
        R.sweep(self.root, cycles=[self.a1["cycle_id"]], invoke=Recorder())
        campaign = self.meta_doc(self.a1)["campaign"]
        self.assertEqual((campaign["title"], campaign["source"]["title"]["by"]), ("사람이 선언한 제목", "human"))
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(self.meta_doc(self.a1)["cycles"][self.a1["cycle_id"]]["title"], "쉬운 제목")

    def test_an_explicit_backfill_shows_and_renews_the_old_declaration_title(self):
        path = self.root / repair.DISPLAY_TITLE_REL
        path.write_text(json.dumps({"schema": repair.DECLARATION_SCHEMA, "artifact_root_id": fixture.ROOT_ID, "entries": [{
            "campaign_id": self.camp, "campaign_locator": "x", "display_title": "옛 선언 제목",
            "manifest_bindings": [], "manifest_revision_ids": [], "manifest_digests": []}]}, ensure_ascii=False),
            encoding="utf-8")
        before = path.read_bytes()
        invoke = Recorder()
        R.sweep(self.root, cycles=[self.a1["cycle_id"]], invoke=invoke, replace_legacy_titles=True)
        data = data_of(invoke.prompts[0])
        self.assertEqual(data["campaign_meta"]["previous_title"], "옛 선언 제목")
        self.assertNotIn("title", data["protected_fields"]["campaign"])
        campaign = self.meta_doc(self.a1)["campaign"]
        self.assertEqual((campaign["title"], campaign["source"]["title"]["by"]), ("쉬운 제목", "model"))
        self.assertEqual(path.read_bytes(), before)

    def test_a_campaign_whose_cycles_all_have_metadata_is_still_asked_once_to_renew_the_title(self):
        path = self.root / repair.DISPLAY_TITLE_REL
        path.write_text(json.dumps({"schema": repair.DECLARATION_SCHEMA, "artifact_root_id": fixture.ROOT_ID, "entries": [{
            "campaign_id": self.camp, "campaign_locator": "x", "display_title": "옛 선언 제목",
            "manifest_bindings": [], "manifest_revision_ids": [], "manifest_digests": []}]}, ensure_ascii=False),
            encoding="utf-8")
        # a run without the option fills every sealed cycle and copies the old title as a person's
        R.sweep(self.root, campaign_ids=[self.camp], invoke=Recorder())
        campaign = self.meta_doc(self.a1)["campaign"]
        self.assertEqual((campaign["title"], campaign["source"]["title"]["by"]), ("옛 선언 제목", "human"))
        quiet = Recorder()
        R.sweep(self.root, campaign_ids=[self.camp], invoke=quiet, missing_only=True)
        self.assertEqual(len(quiet.prompts), 0)
        renew = Recorder()
        R.sweep(self.root, campaign_ids=[self.camp], invoke=renew, missing_only=True, replace_legacy_titles=True)
        self.assertEqual(len(renew.prompts), 1)
        self.assertEqual(data_of(renew.prompts[0])["campaign_meta"].get("previous_title"), "옛 선언 제목")
        campaign = self.meta_doc(self.a1)["campaign"]
        self.assertEqual((campaign["title"], campaign["source"]["title"]["by"]), ("쉬운 제목", "model"))

    def test_a_cycle_whose_folder_is_gone_is_neither_a_target_nor_a_member_candidate(self):
        gone = self.seal(slug="gone")
        record = P.read_cycle_record(self.root, gone["cycle_id"])
        shutil.rmtree(P.cycle_dir(self.root, record["campaign_id"], gone["cycle_id"], record))
        invoke = Recorder()
        result = R.sweep(self.root, campaign_ids=[gone["campaign_id"]], invoke=invoke)
        self.assertIn({"cycle_id": gone["cycle_id"], "reason": "cycle-folder-missing"}, result["skipped"])
        for prompt in invoke.prompts:
            data = data_of(prompt)
            offered = {row["cycle_id"] for row in data["targets"]} | {
                row["cycle_id"] for row in data["ungrouped_context"]}
            self.assertNotIn(gone["cycle_id"], offered)

    def test_the_project_language_follows_the_titles_it_already_shows(self):
        path = self.root / repair.DISPLAY_TITLE_REL
        path.write_text(json.dumps({"schema": repair.DECLARATION_SCHEMA, "artifact_root_id": fixture.ROOT_ID, "entries": [{
            "campaign_id": self.camp, "campaign_locator": "x", "display_title": "한국어 제목",
            "manifest_bindings": [], "manifest_revision_ids": [], "manifest_digests": []}]}, ensure_ascii=False),
            encoding="utf-8")
        R._project_language.cache_clear()
        self.addCleanup(R._project_language.cache_clear)
        invoke = Recorder()
        R.sweep(self.root, cycles=[self.a1["cycle_id"]], invoke=invoke)
        self.assertEqual(data_of(invoke.prompts[0])["project_meta"]["language"], "Korean")

    def test_the_backfill_options_are_refused_on_an_automatic_run(self):
        for extra in (["--replace-legacy-titles"], ["--campaign", self.camp]):
            with self.subTest(extra):
                err = io.StringIO()
                with redirect_stderr(err), redirect_stdout(io.StringIO()):
                    code = R.main(["sweep", "--artifact-root", str(self.root), "--auto", *extra])
                self.assertEqual(code, 65)
                self.assertIn("auto-explicit-only-option", err.getvalue())

    def test_dry_run_still_asks_the_model_but_writes_nothing_anywhere(self):
        before = tree_snapshot(self.root)
        invoke = Recorder()
        result = R.sweep(self.root, cycles=[self.a1["cycle_id"], self.b1["cycle_id"]], dry_run=True, invoke=invoke)
        self.assertEqual(tree_snapshot(self.root), before)
        self.assertEqual(len(invoke.prompts), 2)
        report = self.report(result)
        self.assertEqual((report["status"], bool(report["metadata_diff"])), ("dry-run", True))
        self.assertIn("campaign.title", report["metadata_fields"])

    def test_the_two_switches_have_their_own_reach(self):
        sealed = self.seal(slug="switch")
        cases = (({R.DISABLE_ENV: "off"}, "disabled"), ({M.TITLE_DISABLE_ENV: "off"}, "no-title"),
                 ({R.DISABLE_ENV: "off", M.TITLE_DISABLE_ENV: "off"}, "disabled"), ({}, "all"))
        for env, expected in cases:
            with self.subTest(env), mock.patch.dict(os.environ, env):
                for path in (self.root / M.STATE_REL, self.root / M.PROJECT_REL, self.root / H.HISTORY_REL):
                    if path.is_dir():
                        shutil.rmtree(path)
                    elif path.exists():
                        path.unlink()
                for begun in (self.a1, self.b1, sealed):
                    meta = self.root / M._campaign_location(self.root, begun["campaign_id"])[1]
                    meta.unlink(missing_ok=True)
                R.record_path(self.root).unlink(missing_ok=True)
                invoke = Recorder()
                result = R.sweep(self.root, auto=True, cycles=[sealed["cycle_id"]], invoke=invoke)
                if expected == "disabled":
                    self.assertEqual((result["status"], invoke.prompts), ("disabled", []))
                    continue
                doc = self.meta_doc(sealed)
                self.assertEqual("title" in doc["campaign"], expected == "all")
                self.assertTrue(doc["cycles"][sealed["cycle_id"]]["title"])  # cycle metadata is never held back
                self.assertEqual(doc["campaign"]["summary"], "한 일과 결과를 한 줄로 적음")
                # an explicit sweep and a person's edit ignore the title switch
                explicit = R.sweep(self.root, cycles=[sealed["cycle_id"]], invoke=Recorder())
                self.assertEqual(explicit["status"], "ok")
                self.assertIn("title", self.meta_doc(sealed)["campaign"])

    def test_campaign_selector_and_explicit_everything_sweeps_reach_old_cycles_once(self):
        old = self.seal(slug="old", now=PAST)
        invoke = Recorder()
        result = R.sweep(self.root, campaign_ids=[self.camp], invoke=invoke)
        self.assertEqual(len(invoke.prompts), 1)
        self.assertEqual(sorted(invoke.targets[0]), sorted([self.a1["cycle_id"], self.a2["cycle_id"], old["cycle_id"]]))
        self.assertEqual(self.report(result)["campaign_id"], self.camp)
        every = Recorder()
        R.sweep(self.root, since=R.MIN_STAMP, missing_only=True, invoke=every)
        self.assertEqual([data_of(p)["campaign"]["campaign_id"] for p in every.prompts], [self.other])  # nothing else is missing
        again = Recorder()
        R.sweep(self.root, campaign_ids=[self.camp], missing_only=True, invoke=again)
        self.assertEqual(again.prompts, [])  # a fill-in run costs nothing the second time
        plain = Recorder()
        R.sweep(self.root, campaign_ids=[self.camp], invoke=plain)  # an explicit sweep without it judges again
        self.assertEqual(len(plain.prompts), 1)

    def new_group_reply(self, cycle_id):
        def reply(prompt):
            base = json.loads(none_for(prompt))
            base["decisions"] = [{"cycle_id": cycle_id, "verdict": "new", "new_group": "g1", "stage_label": "S",
                                  "reason": "새 하위 목표"}]
            base["new_groups"] = [{"key": "g1", "title": "새 하위 목표", "members": [{"cycle_id": cycle_id,
                                                                                    "stage_label": "S"}]}]
            return json.dumps(base, ensure_ascii=False)
        return reply

    def test_a_new_group_and_the_metadata_are_one_change_with_one_history_transaction(self):
        report = self.report(R.sweep(self.root, cycles=[self.a1["cycle_id"]], invoke=Recorder(self.new_group_reply(self.a1["cycle_id"]))))
        self.assertEqual(report["status"], "applied")
        group_id = next(iter(report["new_group_ids"].values()))
        events = list(H.iter_events(self.root))
        groups = [e for e in events if e["kind"] == "group"]
        self.assertEqual([(e["target"]["id"], e["operation"], e["actor"]["by"]) for e in groups], [(group_id, "add", "model")])
        self.assertEqual(groups[0]["field"], f"groups.{group_id}")
        self.assertTrue(groups[0]["target"]["path"].endswith("/workflow-groups.json"))
        metas = [e for e in events if e["kind"] == "meta"]
        self.assertEqual({e["transaction_id"] for e in metas}, {groups[0]["transaction_id"]})  # one signal, one transaction
        self.assertEqual(self.declaration(self.camp)["groups"][0]["group_id"], group_id)
        self.assertEqual(self.record()["cycles"][self.a1["cycle_id"]]["verdict"], "new-group")

    def test_an_interruption_after_the_commit_point_leaves_group_and_metadata_for_the_next_write_to_finish(self):
        real = P._write_atomic

        def failing(path, data, mode=0o644):
            if Path(path).name == Path(M.STATE_REL).name:
                raise OSError("injected disk failure")
            return real(path, data, mode)

        with mock.patch.object(P, "_write_atomic", side_effect=failing):
            report = self.report(R.sweep(self.root, cycles=[self.a1["cycle_id"]],
                                         invoke=Recorder(self.new_group_reply(self.a1["cycle_id"]))))
        self.assertEqual((report["status"], report["failure_class"]), ("failed", "apply-failed"))
        self.assertEqual(len(M.pending_intents(self.root)), 1)
        self.assertEqual(len(self.declaration(self.camp)["groups"]), 1)  # both files were already replaced
        self.assertEqual(M.read_campaign_meta(self.root, self.camp).status, "ok")
        self.assertEqual(list(H.iter_events(self.root)), [])  # the lines wait for the recovery
        M.run_write(self.root, lambda ws: M.op_set(ws, self.other, None, {"summary": "다음 쓰기"}), now=1_790_000_000.0)
        self.assertEqual(M.pending_intents(self.root), [])
        events = list(H.iter_events(self.root))
        self.assertEqual(len([e for e in events if e["kind"] == "group"]), 1)
        self.assertEqual(len({e["event_id"] for e in events}), len(events))
        self.assertTrue(M.read_campaign_meta(self.root, self.camp).doc["campaign"]["short_id"].startswith("GEN-"))
        self.assertEqual(self.record()["cycles"][self.a1["cycle_id"]]["failure_class"], "apply-failed")

    def test_a_member_of_a_group_and_a_new_cycle_in_one_campaign_share_the_call(self):
        self.declare(self.camp, [{"title": "Existing", "members": [
            {"cycle_id": self.a1["cycle_id"], "stage_label": "One"}], "relations": []}])
        invoke = Recorder()
        R.sweep(self.root, auto=True, cycles=[self.a1["cycle_id"], self.a2["cycle_id"]], invoke=invoke)
        self.assertEqual(len([p for p in invoke.prompts if data_of(p)["campaign"]["campaign_id"] == self.camp]), 1)
        self.assertEqual(self.record()["cycles"][self.a1["cycle_id"]]["verdict"], "member")
        self.assertEqual(self.record()["cycles"][self.a2["cycle_id"]]["verdict"], "unassigned")


class ModelCallTest(unittest.TestCase):
    def test_invoke_model_uses_the_light_profile_stdin_and_neutral_workdir(self):  # T12
        rt = R._refresh_title()
        prompt = "P" * 5000
        cascade = mock.Mock(return_value=("reply", 1))
        governor = mock.Mock()
        governor.acquire.return_value = "token"
        with tempfile.TemporaryDirectory() as state, \
                mock.patch.dict(os.environ, {"XDG_STATE_HOME": state, "AGENT_DISPATCH_ATTEMPT_ID": "att-x",
                                             "AGENT_ROUTE_ID": "rt-x", "FLEET_TITLE_PROVIDER": "codex",
                                             "AGENT_ARTIFACT_WORKFLOW_GROUP_ID": "wg-x"}), \
                mock.patch.object(rt, "selected_providers", return_value=("claude", "opencode")) as select, \
                mock.patch.object(rt, "provider_model", return_value="model-x") as model, \
                mock.patch.object(rt, "_executable_available", return_value=True), \
                mock.patch.object(rt, "run_provider_cascade", cascade), \
                mock.patch.object(R, "_load_governor", return_value=governor):
            text, harness = R._invoke_model(prompt)
            workdir = R.neutral_workdir()
            self.assertTrue((workdir / ".opencode" / "agent" / "workflow-group-reviewer.md").is_file())
        self.assertEqual((text, harness), ("reply", "opencode"))
        select.assert_called_once_with(profile="light", pin_env=None)
        self.assertTrue(all(call.kwargs["profile"] == "light" for call in model.call_args_list))
        commands = cascade.call_args.args[0]
        self.assertEqual(len(commands), 2)
        claude_argv, claude_stdin, _out = commands[0]
        self.assertNotIn(prompt, claude_argv)
        self.assertEqual((claude_argv[:2], claude_stdin), (["claude", "-p"], prompt))
        opencode_argv = commands[1][0]
        self.assertEqual(opencode_argv[opencode_argv.index("--agent") + 1], "workflow-group-reviewer")
        self.assertEqual(cascade.call_args.kwargs["cwd"], str(workdir))
        env = cascade.call_args.kwargs["env"]
        self.assertEqual(env["AGENT_SESSION_ROLE"], "worker")
        self.assertNotIn("AGENT_DISPATCH_ATTEMPT_ID", env)
        self.assertNotIn("AGENT_ROUTE_ID", env)
        self.assertNotIn("AGENT_ARTIFACT_WORKFLOW_GROUP_ID", env)
        governor.acquire.assert_called_once()
        self.assertEqual(governor.acquire.call_args.args[1], "title")
        governor.release.assert_called_once()

    def test_invoke_model_passes_a_caller_agent_out_tag_and_label(self):
        rt = R._refresh_title()
        governor = mock.Mock()
        governor.acquire.return_value = "token"
        with tempfile.TemporaryDirectory() as state, \
                mock.patch.dict(os.environ, {"XDG_STATE_HOME": state}), \
                mock.patch.object(rt, "selected_providers", return_value=("claude",)), \
                mock.patch.object(rt, "provider_command", return_value=(["claude", "-p"], "P", "out")) as command, \
                mock.patch.object(rt, "_executable_available", return_value=True), \
                mock.patch.object(rt, "run_provider_cascade", return_value=("reply", 0)), \
                mock.patch.object(R, "_load_governor", return_value=governor):
            result = R._invoke_model("P", agent=("x", "y"), out_tag="campaign-title", label="campaign-title")
        self.assertEqual(result, ("reply", "claude"))
        self.assertEqual(command.call_args.kwargs["opencode_agent"], ("x", "y"))
        self.assertEqual(command.call_args.kwargs["out_tag"], "campaign-title")
        self.assertEqual(command.call_args.kwargs["profile"], "light")
        governor.acquire.assert_called_once()
        self.assertEqual(governor.acquire.call_args.args[1], "title")
        self.assertEqual(governor.acquire.call_args.kwargs["label"], "campaign-title")

    def test_no_provider_or_governor_failure_is_unavailable(self):
        rt = R._refresh_title()
        with mock.patch.object(rt, "selected_providers", return_value=()):
            self.assertEqual(R._invoke_model("p"), ("", None))
        with tempfile.TemporaryDirectory() as state, mock.patch.dict(os.environ, {"XDG_STATE_HOME": state}), \
                mock.patch.object(rt, "selected_providers", return_value=("claude",)), \
                mock.patch.object(rt, "provider_model", return_value="m"), \
                mock.patch.object(rt, "_executable_available", return_value=True), \
                mock.patch.object(R, "_load_governor", side_effect=ImportError("nope")):
            self.assertEqual(R._invoke_model("p"), ("", None))


class CliTest(unittest.TestCase):
    def test_help_and_argument_errors(self):
        out = io.StringIO()
        with redirect_stdout(out), self.assertRaises(SystemExit) as caught:
            R.main(["sweep", "--help"])
        self.assertEqual(caught.exception.code, 0)
        self.assertIn("--dry-run", out.getvalue())
        for argv in (["sweep"], ["sweep", "--artifact-root", "/nonexistent-root"],
                     ["sweep", "--artifact-root", "/tmp", "--since", "yesterday"]):
            with redirect_stderr(io.StringIO()):
                try:
                    code = R.main(argv)
                except SystemExit as exc:
                    code = exc.code
            self.assertEqual(code, 65, argv)


if __name__ == "__main__":
    unittest.main()
