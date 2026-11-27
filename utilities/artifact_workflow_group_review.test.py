#!/usr/bin/env python3
"""Tests for `artifact_workflow_group_review.py`: target selection, response validation,
metadata apply, judgement record, dry-run, seal trigger, and isolation.  The review judges
titles and summaries only; it never reads or writes a workflow group declaration.

Real activate/begin/close/finalize fixtures come from `artifact_producer.test.py`. The
model is never called: every test injects `invoke` or mocks the provider cascade.
"""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
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
GROUP_LIMIT = 32  # the most groups one declaration holds (`artifact_workflow_groups.prepare`)
START = "=== CAMPAIGN DATA ===\n"
END = "\n=== END DATA ==="


def data_of(prompt):
    return json.loads(prompt.split(START, 1)[1].rsplit(END, 1)[0])


def target_ids(prompt):
    return [row["cycle_id"] for row in data_of(prompt)["targets"]]


def meta_for(prompt, title="쉬운 제목", summary="한 일과 결과를 한 줄로 적음"):
    """A valid v2 `metadata` + `new_branches` pair for whatever the prompt asks (the model is never called)."""
    data = data_of(prompt)
    vocab = [item["code"] for item in data["project_meta"]["branches"]]
    entity = {"title": title, "summary": summary, "branches": [vocab[0] if vocab else "GEN"], "kinds": ["평가"]}
    return {"metadata": {"campaign": dict(entity), "cycles": {cid: dict(entity) for cid in data["metadata_target_ids"]}},
            "new_branches": [] if vocab else [{"code": "GEN", "label": "일반", "note": "기본 갈래"}]}


def reply_for(prompt):
    """A valid metadata-only answer for whatever the prompt asks."""
    return json.dumps(meta_for(prompt), ensure_ascii=False)


def answer(payload):
    """An `invoke` answering `payload` (a string as it is, anything else as JSON)."""
    def invoke(prompt):
        return (payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)), "claude"
    return invoke


class Recorder:
    """An `invoke` that records each prompt and answers valid metadata for its targets."""

    def __init__(self, reply=None):
        self.prompts = []
        self.reply = reply or reply_for

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

    def test_group_members_are_judged_like_any_other_cycle_and_the_declaration_is_untouched(self):  # T2
        a, b, c = (self.seal(slug=name) for name in ("a", "b", "c"))
        self.declare(a["campaign_id"], [{"title": "Existing", "members": [
            {"cycle_id": a["cycle_id"], "stage_label": "One"},
            {"cycle_id": b["cycle_id"], "stage_label": "Two"}], "relations": []}])
        declared = W.declaration_path(self.root, a["campaign_id"]).read_bytes()
        invoke = Recorder()
        result = R.sweep(self.root, cycles=[a["cycle_id"], c["cycle_id"]], invoke=invoke)
        self.assertEqual([sorted(ids) for ids in invoke.targets], [sorted([a["cycle_id"], c["cycle_id"]])])
        self.assertNotIn("already_member", result)
        self.assertEqual(self.record()["cycles"][a["cycle_id"]]["verdict"], "titled")
        auto = Recorder()
        R.sweep(self.root, auto=True, cycles=[b["cycle_id"]], invoke=auto)
        self.assertEqual(auto.targets, [[b["cycle_id"]]])
        auto = Recorder()
        R.sweep(self.root, auto=True, cycles=[b["cycle_id"]], invoke=auto)
        self.assertEqual(auto.prompts, [])  # judged once: its record says `titled`
        self.assertEqual(W.declaration_path(self.root, a["campaign_id"]).read_bytes(), declared)

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
        self.assertEqual(self.record()["cycles"][cid]["verdict"], "titled")

    def test_open_titled_is_reassessed_once_after_sealing(self):  # T3
        seed = self.seal(slug="seed")
        R.sweep(self.root, auto=True, cycles=[seed["cycle_id"]], invoke=Recorder())
        route, route_file, begun = self.start(slug="opened")
        cid = begun["cycle_id"]
        R.sweep(self.root, cycles=[cid], include_open=True, invoke=Recorder())
        entry = self.record()["cycles"][cid]
        self.assertEqual((entry["verdict"], entry["cycle_state"]), ("titled", "open"))
        self.finish(route, route_file, begun)
        again = Recorder()
        R.sweep(self.root, auto=True, invoke=again)
        self.assertIn(cid, sum(again.targets, []))
        entry = self.record()["cycles"][cid]
        self.assertEqual((entry["verdict"], entry["cycle_state"]), ("titled", "sealed"))
        for _ in range(2):
            later = Recorder()
            R.sweep(self.root, auto=True, invoke=later)
            self.assertNotIn(cid, sum(later.targets, []))

    def test_old_group_verdicts_are_read_but_never_reselected(self):
        """A record written before the review dropped groups is not judged again."""
        old = [self.seal(slug=name, now=PAST + i) for i, name in enumerate(("joined", "new", "member", "none"))]
        trigger = self.seal(slug="trigger")
        R.sweep(self.root, auto=True, cycles=[trigger["cycle_id"]], invoke=Recorder())  # enrolls the root
        verdicts = ("joined", "new-group", "member", "unassigned")
        items = [R.Outcome(cycle_id=cycle["cycle_id"], campaign_id=cycle["campaign_id"], verdict=verdict,
                           group_id="wgrp_" + "a" * 32 if verdict in ("joined", "new-group") else None,
                           cycle_state="sealed") for cycle, verdict in zip(old, verdicts)]
        self.assertTrue(R.record_outcomes(self.root, items, mode="auto", now=None))
        _status, doc = R.read_record(self.root)
        picked = R.select_targets(self.root, doc, auto=True, cycles=[item.cycle_id for item in items])
        self.assertEqual(picked.by_campaign, {})
        later = Recorder()
        R.sweep(self.root, auto=True, invoke=later)
        self.assertFalse({c["cycle_id"] for c in old} & set(sum(later.targets, [])))

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
        self.camp = self.a1["campaign_id"]
        self.groups = self.declare(self.camp, [{"title": "Existing", "members": [
            {"cycle_id": self.a1["cycle_id"], "stage_label": "One"},
            {"cycle_id": self.a2["cycle_id"], "stage_label": "Two"}], "relations": []}])
        self.declared = W.declaration_path(self.root, self.camp).read_bytes()
        self.tid = self.target["cycle_id"]

    def run_reply(self, reply):
        return R.sweep(self.root, cycles=[self.tid], invoke=answer(reply))

    def run_mutated(self, mutate):
        def invoke(prompt):
            return json.dumps(mutate(meta_for(prompt)), ensure_ascii=False), "claude"
        return R.sweep(self.root, cycles=[self.tid], invoke=invoke)

    def test_invalid_responses_reject_the_campaign_and_keep_the_declaration(self):  # T4
        old_keys = {"decisions": [{"cycle_id": "cyc_x", "verdict": "none", "reason": "r"}], "new_groups": [],
                    "relations": []}
        cases = {
            "old-group-keys": lambda v: {**v, **old_keys},
            "old-shape-only": lambda v: dict(old_keys),
            "one-old-key": lambda v: {**v, "relations": []},
            "long-title": lambda v: {**v, "metadata": {**v["metadata"], "campaign": {
                **v["metadata"]["campaign"], "title": "t" * 121}}},
            "control-char": lambda v: {**v, "metadata": {**v["metadata"], "campaign": {
                **v["metadata"]["campaign"], "summary": "bad\x01summary"}}},
        }
        for label, mutate in cases.items():
            with self.subTest(label):
                report = self.report(self.run_mutated(mutate))
                self.assertEqual((report["status"], report["failure_class"]), ("failed", "invalid-response"))
                self.assertEqual(W.declaration_path(self.root, self.camp).read_bytes(), self.declared)
                self.assertEqual(self.record()["cycles"][self.tid]["failure_class"], "invalid-response")
                self.assertEqual(M.read_campaign_meta(self.root, self.camp).status, "missing")
        self.assertEqual(self.report(self.run_mutated(lambda v: v))["status"], "applied")
        for label, text in (("duplicate-key", '{"metadata":{},"metadata":{},"new_branches":[]}'),
                            ("not-json", "here you go: {}")):
            with self.subTest(label):
                self.assertEqual(self.report(self.run_reply(text))["failure_class"], "invalid-response")

    def test_old_group_keys_are_named_in_the_reason(self):
        report = self.report(self.run_mutated(lambda v: {**v, "decisions": []}))
        self.assertEqual(report["detail"], "top-level-keys")
        self.assertEqual(self.record()["cycles"][self.tid]["reason"], "top-level-keys")

    def test_fenced_reply_with_a_trailing_note_is_read(self):
        def invoke(prompt):
            text = "```json\n%s\n```\n\nNote: the title follows the final report." % reply_for(prompt)
            return text, "claude"
        report = self.report(R.sweep(self.root, cycles=[self.tid], invoke=invoke))
        self.assertEqual(report["status"], "applied")
        self.assertEqual(self.record()["cycles"][self.tid]["verdict"], "titled")

    def test_the_prompt_never_asks_for_groups(self):
        text = R.PROMPT_TEMPLATE.lower()
        for word in ("group", "relation", "decisions", "evidence"):
            self.assertNotIn(word, text)

    def test_declaration_bytes_survive_a_review_even_with_relations_and_evidence(self):  # T6
        first, second = self.groups[0]["members"][0]["cycle_id"], self.groups[0]["members"][1]["cycle_id"]
        plan = W.prepare(self.root, self.camp, {"groups": [{
            "group_id": self.groups[0]["group_id"], "title": "Existing", "members": [
                {"cycle_id": first, "stage_label": "One"}, {"cycle_id": second, "stage_label": "Two"}],
            "relations": [{"from_cycle_id": first, "to_cycle_id": second, "kind": "precedes",
                           "rationale": "Two uses one.", "evidence_refs": [
                               {"path": self.path_of(self.a1)}, {"path": self.path_of(self.a2)}]}]}]})
        W.apply(self.root, plan)
        declared = W.declaration_path(self.root, self.camp).read_bytes()
        extra = self.seal(slug="extra")
        protected = [P.campaign_dir(self.root, self.camp) / "campaign.json"]
        protected += list(P.campaign_dir(self.root, self.camp).rglob("manifest.json"))
        protected += list(P.campaign_dir(self.root, self.camp).rglob(".cycle.json"))
        protected = [path for path in protected if path.exists()]
        untouched = {path: path.read_bytes() for path in protected}
        result = R.sweep(self.root, cycles=[self.tid, extra["cycle_id"], first], invoke=Recorder())
        self.assertEqual(self.report(result)["status"], "applied")
        self.assertEqual(W.declaration_path(self.root, self.camp).read_bytes(), declared)
        self.assertEqual({path: path.read_bytes() for path in protected}, untouched)
        self.assertEqual(len(self.declaration(self.camp)["groups"]), 1)
        self.assertEqual(W.verify(self.root, self.camp)["stale_evidence"], [])
        doc = M.read_campaign_meta(self.root, self.camp).doc
        self.assertEqual(doc["campaign"]["title"], "쉬운 제목")
        self.assertTrue(all(doc["cycles"][cid]["summary"] for cid in (self.tid, extra["cycle_id"], first)))
        cycles = self.record()["cycles"]
        self.assertEqual({cycles[cid]["verdict"] for cid in (self.tid, extra["cycle_id"], first)}, {"titled"})

    def test_a_broken_declaration_does_not_hold_back_the_title(self):
        W.declaration_path(self.root, self.camp).write_text("{broken", encoding="utf-8")
        report = self.report(R.sweep(self.root, cycles=[self.tid], invoke=Recorder()))
        self.assertEqual(report["status"], "applied")
        self.assertEqual(W.declaration_path(self.root, self.camp).read_text(encoding="utf-8"), "{broken")

    def test_success_records_the_verdict_and_no_group_fields(self):  # T7
        other = self.seal(key="lonely", slug="lonely")
        result = R.sweep(self.root, cycles=[other["cycle_id"]], invoke=Recorder())
        self.assertEqual(self.report(result)["status"], "applied")
        self.assertFalse(W.declaration_path(self.root, other["campaign_id"]).exists())
        self.assertEqual(M.read_campaign_meta(self.root, other["campaign_id"]).status, "ok")
        entry = self.record()["cycles"][other["cycle_id"]]
        self.assertEqual((entry["verdict"], entry["profile"], entry["harness"], entry["mode"]),
                         ("titled", "light", "claude", "explicit"))
        self.assertEqual((entry["group_id"], entry["stage_label"], entry["reason"], entry["failure_class"]),
                         (None, None, None, None))
        self.assertIsNone(re.search(r"opus|sonnet|haiku|gpt|fable", R.record_path(self.root).read_text(), re.I))

    def test_dry_run_writes_nothing_under_the_artifact_root(self):  # T8
        before = tree_snapshot(self.root)
        result = R.sweep(self.root, cycles=[self.tid], dry_run=True, invoke=Recorder())
        self.assertEqual(tree_snapshot(self.root), before)
        report = self.report(result)
        self.assertEqual(report["status"], "dry-run")
        self.assertNotIn("after_sha256", report)
        self.assertNotIn("decisions", report)
        self.assertFalse(R.record_path(self.root).exists())
        self.assertFalse(R.lock_path(self.root).exists())
        self.assertFalse(R.pending_dir(self.root).exists())

    def test_apply_failure_is_a_hard_failure_and_its_reason_is_recorded(self):
        with mock.patch.object(M, "apply_judgement", side_effect=M.MetaError("meta-write-refused")):
            report = self.report(self.run_mutated(lambda v: v))
        self.assertEqual((report["status"], report["failure_class"], report["detail"]),
                         ("failed", "apply-failed", "meta-write-refused"))
        entry = self.record()["cycles"][self.tid]
        self.assertEqual((entry["hard_failures"], entry["reason"]), (1, "meta-write-refused"))

    def test_an_empty_or_unparseable_answer_records_its_reason(self):
        for text, failure_class, reason in (("", "unavailable", "empty-response"),
                                            ("no object here", "invalid-response", "json-invalid")):
            with self.subTest(reason):
                report = self.report(self.run_reply(text))
                self.assertEqual((report["failure_class"], report["detail"]), (failure_class, reason))
                entry = self.record()["cycles"][self.tid]
                self.assertEqual((entry["verdict"], entry["failure_class"], entry["reason"]),
                                 ("failed", failure_class, reason))

    def test_a_campaign_at_the_group_limit_still_gets_its_title_and_summary(self):  # the 2026-11-17 failure
        members = [self.seal(key="full", slug=f"m{i}", now=PAST + i) for i in range(GROUP_LIMIT)]
        full = members[0]["campaign_id"]
        self.declare(full, [{"title": f"G{i}", "members": [{"cycle_id": m["cycle_id"], "stage_label": "S"}],
                             "relations": []} for i, m in enumerate(members)])
        declared = W.declaration_path(self.root, full).read_bytes()
        target = self.seal(key="full", slug="target")
        invoke = Recorder()
        report = self.report(R.sweep(self.root, cycles=[target["cycle_id"]], invoke=invoke))
        self.assertEqual(report["status"], "applied")
        self.assertEqual(len(invoke.prompts), 1)
        doc = M.read_campaign_meta(self.root, full).doc
        entry = doc["cycles"][target["cycle_id"]]
        self.assertEqual((entry["title"], entry["summary"]), ("쉬운 제목", "한 일과 결과를 한 줄로 적음"))
        self.assertEqual(W.declaration_path(self.root, full).read_bytes(), declared)
        self.assertEqual(len(self.declaration(full)["groups"]), GROUP_LIMIT)
        self.assertEqual(self.record()["cycles"][target["cycle_id"]]["verdict"], "titled")


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
    "old-decisions-key": lambda v: {**v, "decisions": []},
    "old-groups-key": lambda v: {**v, "new_groups": [], "relations": []},
    "new-branch-extra-key": lambda v: {**v, "new_branches": [{**v["new_branches"][0], "extra": 1}]},
}


class UnifiedReviewTest(ReviewBase):
    """One model call per campaign decides the metadata; failures write nothing."""

    def setUp(self):
        super().setUp()
        self.a1 = self.seal(slug="a1")
        self.a2 = self.seal(slug="a2")
        self.b1 = self.seal(key="other", slug="b1")
        self.camp, self.other = self.a1["campaign_id"], self.b1["campaign_id"]

    def review_events(self):
        """What the review wrote to the recorder; the close lines of the fixtures' own cycles are not its."""
        return [e for e in H.iter_events(self.root) if e["kind"] in ("meta", "group")]

    def meta_doc(self, begun):
        read = M.read_campaign_meta(self.root, begun["campaign_id"])
        self.assertEqual(read.status, "ok", read.code)
        return read.doc

    def test_one_call_per_campaign_decides_the_metadata(self):
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

    def test_the_prompt_carries_vocabulary_protected_fields_and_the_target_list(self):
        invoke = Recorder()
        R.sweep(self.root, cycles=[self.a1["cycle_id"]], invoke=invoke)
        data = data_of(invoke.prompts[0])
        self.assertEqual(data["metadata_target_ids"], [self.a1["cycle_id"]])
        self.assertEqual(set(data), {"campaign", "targets", "project_meta", "campaign_meta", "cycle_meta",
                                     "protected_fields", "metadata_target_ids"})
        self.assertEqual(set(data["project_meta"]), {"display_name", "branches", "kinds", "general_branch_limit",
                                                     "new_branch_allowance"})
        self.assertEqual((data["project_meta"]["kinds"], data["project_meta"]["new_branch_allowance"]),
                         (list(M.KINDS), 12))
        self.assertEqual(data["protected_fields"], {"campaign": [], "cycles": {self.a1["cycle_id"]: []}})
        self.assertNotIn("group", invoke.prompts[0].split(START, 1)[0].lower())  # the rules never mention groups
        # a person's fields are named as protected in the next call
        M.run_write(self.root, lambda ws: M.op_set(ws, self.camp, None, {"summary": "사람 요약"}), now=1_790_000_000.0)
        again = Recorder()
        R.sweep(self.root, cycles=[self.a2["cycle_id"]], invoke=again)
        self.assertEqual(data_of(again.prompts[0])["protected_fields"]["campaign"], ["summary"])
        self.assertEqual(data_of(again.prompts[0])["campaign_meta"]["summary"], "사람 요약")
        self.assertEqual(data_of(again.prompts[0])["project_meta"]["new_branch_allowance"], 1)

    def test_group_members_and_new_cycles_get_metadata_and_the_declaration_is_untouched(self):
        context = self.seal(slug="context")
        self.declare(self.camp, [{"title": "Existing", "members": [
            {"cycle_id": self.a1["cycle_id"], "stage_label": "One"}], "relations": []}])
        declared = W.declaration_path(self.root, self.camp).read_bytes()
        invoke = Recorder()
        result = R.sweep(self.root, cycles=[self.a2["cycle_id"], self.a1["cycle_id"]], invoke=invoke)
        self.assertEqual(sorted(target_ids(invoke.prompts[0])), sorted([self.a1["cycle_id"], self.a2["cycle_id"]]))
        self.assertEqual(self.report(result)["status"], "applied")
        self.assertEqual(W.declaration_path(self.root, self.camp).read_bytes(), declared)
        cycles = self.meta_doc(self.a1)["cycles"]
        self.assertEqual(set(cycles), {self.a1["cycle_id"], self.a2["cycle_id"]})  # not the other sealed cycle
        self.assertNotIn(context["cycle_id"], cycles)
        self.assertNotIn(context["cycle_id"], invoke.prompts[0])

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
            return reply_for(prompt), "claude"

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
                    base = json.loads(reply_for(prompt))
                    base["new_branches"] = base["new_branches"] or [{"code": "GEN", "label": "일반", "note": ""}]
                    return json.dumps(mutate(base), ensure_ascii=False)
                before = content_snapshot(self.root)
                report = self.report(R.sweep(self.root, cycles=[self.a1["cycle_id"]], invoke=Recorder(reply)))
                self.assertEqual((report["status"], report["failure_class"]), ("failed", "invalid-response"), report)
                self.assertEqual(content_snapshot(self.root), before)
                self.assertEqual(M.read_campaign_meta(self.root, self.camp).status, "missing")
                self.assertFalse((self.root / M.PROJECT_REL).exists())
                self.assertFalse((self.root / M.STATE_REL).exists())
                self.assertEqual(self.review_events(), [])
        self.assertEqual(self.record()["cycles"][self.a1["cycle_id"]]["failure_class"], "invalid-response")

    def test_model_unavailable_a_failed_write_and_a_broken_meta_leave_every_judged_file_alone(self):
        before = content_snapshot(self.root)
        for label, invoke in (("empty", lambda prompt: ("", None)), ("not-json", lambda prompt: ("nope", "claude")),
                              ("raises-as-empty", lambda prompt: ("   ", "claude"))):
            with self.subTest(label):
                report = self.report(R.sweep(self.root, cycles=[self.a1["cycle_id"]], invoke=invoke))
                self.assertEqual(report["status"], "failed")
                self.assertEqual(content_snapshot(self.root), before)
        with mock.patch.object(M, "apply_judgement", side_effect=M.MetaError("meta-write-refused")):
            report = self.report(R.sweep(self.root, cycles=[self.a1["cycle_id"]], invoke=Recorder()))
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
            return reply_for(prompt), "claude"

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

    def test_a_cycle_whose_folder_is_gone_is_not_a_target(self):
        gone = self.seal(slug="gone")
        record = P.read_cycle_record(self.root, gone["cycle_id"])
        shutil.rmtree(P.cycle_dir(self.root, record["campaign_id"], gone["cycle_id"], record))
        invoke = Recorder()
        result = R.sweep(self.root, campaign_ids=[gone["campaign_id"]], invoke=invoke)
        self.assertIn({"cycle_id": gone["cycle_id"], "reason": "cycle-folder-missing"}, result["skipped"])
        for prompt in invoke.prompts:
            data = data_of(prompt)
            self.assertNotIn(gone["cycle_id"], {row["cycle_id"] for row in data["targets"]})

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

    def test_a_group_member_and_a_new_cycle_in_one_campaign_share_the_call(self):
        self.declare(self.camp, [{"title": "Existing", "members": [
            {"cycle_id": self.a1["cycle_id"], "stage_label": "One"}], "relations": []}])
        declared = W.declaration_path(self.root, self.camp).read_bytes()
        invoke = Recorder()
        R.sweep(self.root, auto=True, cycles=[self.a1["cycle_id"], self.a2["cycle_id"]], invoke=invoke)
        self.assertEqual(len([p for p in invoke.prompts if data_of(p)["campaign"]["campaign_id"] == self.camp]), 1)
        self.assertEqual(self.record()["cycles"][self.a1["cycle_id"]]["verdict"], "titled")
        self.assertEqual(self.record()["cycles"][self.a2["cycle_id"]]["verdict"], "titled")
        self.assertEqual(W.declaration_path(self.root, self.camp).read_bytes(), declared)


class ModelCallTest(unittest.TestCase):
    def test_invoke_model_uses_the_light_profile_stdin_and_neutral_workdir(self):  # T12
        rt = R._refresh_title()
        prompt = "P" * 5000
        cascade = mock.Mock(return_value=("reply", 1))
        governor = mock.Mock()
        governor.acquire.return_value = "token"
        governor.title_start_budget.return_value = 20
        with tempfile.TemporaryDirectory() as state, \
                mock.patch.dict(os.environ, {"XDG_STATE_HOME": state, "AGENT_DISPATCH_ATTEMPT_ID": "att-x",
                                             "AGENT_ROUTE_ID": "rt-x", "FLEET_TITLE_PROVIDER": "codex",
                                             "AGENT_ARTIFACT_WORKFLOW_GROUP_ID": "wg-x"}), \
                mock.patch.object(rt, "selected_providers", return_value=("claude", "opencode")) as select, \
                mock.patch.object(rt, "provider_settings", return_value={"model": "model-x", "budget": "low"}) as model, \
                mock.patch.object(rt.opencode_runtime, "is_v2", return_value=False), \
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
        self.assertEqual(governor.acquire.call_args.kwargs["budget"], 20)
        governor.release.assert_called_once()

    def test_invoke_model_v2_uses_its_prefixed_agent_and_neutral_workdir(self):
        rt = R._refresh_title()
        governor = mock.Mock(); governor.acquire.return_value = 'token'; governor.title_start_budget.return_value = 20
        cascade = mock.Mock(return_value=('reply', 0)); prompt = 'Synthetic input only'
        with tempfile.TemporaryDirectory() as state, \
                mock.patch.dict(os.environ, {'XDG_STATE_HOME': state}), \
                mock.patch.object(rt, 'selected_providers', return_value=('opencode',)), \
                mock.patch.object(rt, 'provider_settings', return_value={'model': 'model-x', 'budget': 'low'}), \
                mock.patch.object(rt.opencode_runtime, 'is_v2', return_value=True), \
                mock.patch.object(rt.opencode_runtime, 'select_automatic_model', return_value=('model-x', 'low', {'state': 'available'})), \
                mock.patch.object(rt.opencode_runtime, 'automatic_eligible', return_value=True), \
                mock.patch.object(rt, '_executable_available', return_value=True), \
                mock.patch.object(rt, 'run_provider_cascade', cascade), \
                mock.patch.object(R, '_load_governor', return_value=governor):
            self.assertEqual(R._invoke_model(prompt), ('reply', 'opencode'))
            workdir = R.neutral_workdir()
            self.assertTrue((workdir / '.opencode/agents/hearting-workflow-group-reviewer.md').is_file())
            config = json.loads((workdir / '.opencode/opencode.json').read_text())
            self.assertTrue(all(row['effect'] == 'deny' for row in config['permissions']))
        argv, stdin, _ = cascade.call_args.args[0][0]
        self.assertEqual(argv[argv.index('--agent') + 1], 'hearting-workflow-group-reviewer')
        self.assertNotIn(prompt, argv); self.assertEqual(stdin, prompt)
        self.assertEqual(cascade.call_args.kwargs['cwd'], str(workdir))
        governor.release.assert_called_once()

    def test_invoke_model_passes_a_caller_agent_out_tag_and_label(self):
        rt = R._refresh_title()
        governor = mock.Mock()
        governor.acquire.return_value = "token"
        governor.title_start_budget.return_value = 20
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
        self.assertEqual(governor.acquire.call_args.kwargs["budget"], 20)

    def test_no_provider_or_governor_failure_is_unavailable(self):
        rt = R._refresh_title()
        with mock.patch.object(rt, "selected_providers", return_value=()):
            self.assertEqual(R._invoke_model("p"), ("", None))
        with tempfile.TemporaryDirectory() as state, mock.patch.dict(os.environ, {"XDG_STATE_HOME": state}), \
                mock.patch.object(rt, "selected_providers", return_value=("claude",)), \
                mock.patch.object(rt, "provider_settings", return_value={"model": "m", "budget": "low"}), \
                mock.patch.object(rt, "_executable_available", return_value=True), \
                mock.patch.object(R, "_load_governor", side_effect=ImportError("nope")):
            self.assertEqual(R._invoke_model("p"), ("", None))


FAKE_PROVIDER = (
    "import json, os, sys\n"
    "path = os.path.join(os.environ['FAKE_MODEL_ENV_DIR'], str(os.getpid()) + '.json')\n"
    "with open(path + '.part', 'w') as out:\n"
    "    out.write(json.dumps({'argv': sys.argv[1:], 'env': dict(os.environ)}))\n"
    "os.replace(path + '.part', path)\n"
    "print('{}')\n"
)


class WindowValuesTest(unittest.TestCase):
    def test_the_review_and_campaign_title_models_never_receive_the_starting_windows_session_shell(self):
        """The sealed-cycle review and the campaign-title repair run a model from
        whatever process asked for them, a person's window included; that model gets
        none of the window's `HEARTING_SHELL_*`, and what else the call carries still
        arrives.

        `campaign-title` calls the repair's own model entry. `seal-trigger` captures
        the detached sweep a seal starts and runs the sweep's model call under that
        environment, as the sweep does. Both reach a fake `claude` on PATH through
        the real provider command and cascade; only the provider choice is fixed.
        The window values are made up.
        """
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        fake_bin = base / "fake-bin"
        fake_bin.mkdir()
        seen = base / "model-environments"
        seen.mkdir()
        fake_claude = fake_bin / "claude"
        fake_claude.write_text("#!" + sys.executable + "\n" + FAKE_PROVIDER, encoding="utf-8")
        fake_claude.chmod(0o755)
        root = base / "artifacts"
        (root / R.CUTOVER_REL).parent.mkdir(parents=True)
        (root / R.CUTOVER_REL).write_text("{}\n", encoding="utf-8")
        window = {
            "HEARTING_SHELL_SOCKET": str(base / "fixture-shell" / "control.sock"),
            "HEARTING_SHELL_TOKEN": "f" * 64,
            "HEARTING_SHELL_NATIVE_MODEL": "fixture-window-model",
        }
        caller = {
            **window,
            "PATH": str(fake_bin) + os.pathsep + os.environ.get("PATH", "/usr/bin:/bin"),
            "FAKE_MODEL_ENV_DIR": str(seen),
            "FIXTURE_CARRIED": "kept-by-the-review-model",
            "AGENT_HOME": str(Path(R.__file__).resolve().parents[1]),
            "AGENT_MODEL_GOVERNOR_ROOT": str(base / "governor"),
            "AGENT_MODEL_WORKER_TOTAL": "5",
            "AGENT_MODEL_WORKER_START_BUDGET": "20",
            "XDG_STATE_HOME": str(base / "state"),
            "AGENT_DISPATCH_JOBS": str(base / "jobs.log"),
            "AGENT_ROUTE_ID": "rt-window",
            "AGENT_ARTIFACT_WORKFLOW_GROUP_ID": "wg-window",
        }
        rt = R._refresh_title()

        def campaign_title():
            return repair._default_invoke("Synthetic campaign title input")

        def seal_trigger():
            os.environ.pop(R.DISABLE_ENV, None)  # restored with the subtest's environment
            with mock.patch.object(R, "in_test_process", return_value=False), \
                    mock.patch.object(R.subprocess, "Popen") as popen:
                self.assertTrue(R.launch_after_seal(root, {"cycle_id": "cyc_" + "3" * 32}))
            popen.assert_called_once()
            self.assertEqual(popen.call_args.args[0][2:4], ["sweep", "--artifact-root"])
            with mock.patch.dict(os.environ, dict(popen.call_args.kwargs["env"]), clear=True):
                return R._invoke_model("Synthetic sweep input")

        for name, call in (("campaign-title", campaign_title), ("seal-trigger", seal_trigger)):
            with self.subTest(path=name), mock.patch.dict(os.environ, caller), \
                    mock.patch.object(rt, "selected_providers", return_value=("claude",)):
                before = set(seen.glob("*.json"))
                text, harness = call()
                self.assertEqual((text.strip(), harness), ("{}", "claude"))
                records = [json.loads(path.read_text()) for path in sorted(set(seen.glob("*.json")) - before)]
                self.assertEqual(len(records), 1, "the review never ran its model")
                self.assertEqual(records[0]["argv"][0], "-p")
                received = records[0]["env"]
                self.assertEqual(sorted(key for key in received if key.startswith("HEARTING_SHELL_")), [])
                self.assertEqual(received.get("FIXTURE_CARRIED"), "kept-by-the-review-model")
                # What the review's call already kept or dropped stays as it was.
                self.assertEqual(received.get("AGENT_SESSION_ROLE"), "worker")
                self.assertEqual(received.get("AGENT_DISPATCH_JOBS"), caller["AGENT_DISPATCH_JOBS"])
                self.assertNotIn("AGENT_ROUTE_ID", received)
                self.assertNotIn("AGENT_ARTIFACT_WORKFLOW_GROUP_ID", received)
                self.assertEqual(received.get("HOME"), os.environ.get("HOME"))
                self.assertEqual(received.get("CLAUDE_CODE_DISABLE_AUTO_MEMORY"), "1")
                # The window that asked for it keeps its own values.
                self.assertEqual({key: os.environ.get(key) for key in window}, window)


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
