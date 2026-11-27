#!/usr/bin/env python3
"""``mem tidy-apply`` / ``mem tidy-undo``: the closed apply of the session-tidy worker.

Every subprocess runs inside ``tidy_isolation`` (``/var/tmp`` root for HOME, XDG_*,
MEM_STORE; remote off; no worker or pane markers).  The few in-process cases load a
fresh copy of ``mem`` under ``iso.patched_environ()`` so its store constant points
at the isolated root.
"""

from __future__ import annotations
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../../run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])

import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT / "utilities"))
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "tools" / "memory"))

from tidy_isolation import isolated_env  # noqa: E402

MEM = ROOT / "tools" / "memory" / "mem.py"
OTHER_PROJECT = "git:example.com/other/repo"
ID_RE = re.compile(r"→ (\S+)")
SUMMARY_RE = re.compile(r"^\[tidy\] 묶음 (\S+): .*되돌리기: mem tidy-undo (\S+)$")


def load_fresh_mem(name="mem_fresh"):
    spec = importlib.util.spec_from_file_location(name, MEM)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class TidyApplyCase(unittest.TestCase):

    def setUp(self):
        self.iso = isolated_env()
        self.addCleanup(self.iso.cleanup)
        self.proj = self.iso.root / "proj"
        self.proj.mkdir()
        self.work = self.iso.root / "work"
        self.work.mkdir()
        self.state = self.iso.xdg_state / "hearting" / "session-tidy"
        self.counter = 0

    # -- helpers -----------------------------------------------------------

    def mem(self, *args, cwd=None, extra=None):
        return self.iso.run([sys.executable, MEM, *args], cwd=cwd or self.proj, extra=extra)

    def seed(self, tier, rtype, body, *flags):
        result = self.mem("add", tier, rtype, body, *flags)
        self.assertEqual(result.returncode, 0, result.stderr)
        match = ID_RE.search(result.stdout)
        self.assertTrue(match, result.stdout)
        return match.group(1)

    def rows(self):
        db = self.iso.mem_store / "memory.db"
        if not db.exists():
            return {}
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            return {r[0]: r[1:] for r in con.execute(
                "SELECT id, status, strength, last_accessed, superseded_by, canonical_id, body, type, tier "
                "FROM records")}
        finally:
            con.close()

    def actions(self, actions, batch=None, name="actions.json", **extra):
        self.counter += 1
        doc = {"schema_version": 1, "batch_id": batch or f"b{self.counter}", "actions": actions, **extra}
        path = self.work / name
        path.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
        return path, doc["batch_id"]

    def apply(self, actions, batch=None, input_doc=None, **extra):
        path, batch_id = self.actions(actions, batch=batch, **extra)
        argv = ["tidy-apply", str(path), "--cwd", str(self.proj)]
        if input_doc is not None:
            input_path = self.work / f"input-{batch_id}.json"
            input_path.write_text(json.dumps(input_doc, ensure_ascii=False), encoding="utf-8")
            argv += ["--input", str(input_path)]
        result = self.mem(*argv)
        return result, batch_id

    def result_json(self, batch):
        return json.loads((self.state / "runs" / batch / "result.json").read_text(encoding="utf-8"))

    def last_line(self, result):
        return result.stdout.strip().splitlines()[-1]

    def add_action(self, n, **more):
        return {"kind": "add", "type": "lesson", "tier": "durable",
                "body": f"tidy fixture lesson number {n} about topic {n}", **more}

    def choice(self, n):
        return {"harness": "claude", "call_id": f"toolu_{n}", "question": f"질문 {n}번을 어떻게 할까요?",
                "options": [{"label": "예", "description": "그렇게 한다"}, {"label": "아니오", "description": "안 한다"}],
                "answers": ["예"], "note": "", "asked_at": "2026-09-30T01:00:00Z"}


class ClosedSetTest(TidyApplyCase):

    def test_actions_outside_the_closed_set_are_refused_and_nothing_is_deleted(self):
        keep = self.seed("durable", "lesson", "an existing lesson that must stay exactly where it is")
        before = self.rows()
        result, batch = self.apply([
            {"kind": "delete", "id": keep}, {"kind": "prune", "id": keep},
            {"kind": "merge", "canonical": keep, "ids": [keep]}, {"kind": "explode"}, "not-an-object",
        ])
        self.assertEqual(result.returncode, 0, result.stderr)
        doc = self.result_json(batch)
        reasons = [r["reason"] for r in doc["rejected"]]
        self.assertEqual(reasons, ["forbidden-kind:delete", "forbidden-kind:prune", "forbidden-kind:merge",
                                   "unknown-kind:explode", "not-an-object"])
        self.assertEqual(self.rows(), before)
        self.assertEqual(doc["before"], doc["after"])
        self.assertFalse((self.iso.mem_store / "deleted-records.jsonl").exists())
        self.assertIn("거부 5", self.last_line(result))

    def test_pending_profile_global_other_project_inactive_and_missing_are_protected(self):
        pending = self.seed("working", "handoff", "a handoff nobody consumed yet please keep", "--requires-consume")
        profile = self.seed("durable", "profile", "profile line about how the user likes to work")
        global_id = self.seed("durable", "lesson", "a global lesson belonging to everyone", "--scope", "global")
        other = self.seed("durable", "lesson", "a lesson of another project entirely", "--cwd-origin", OTHER_PROJECT)
        old = self.seed("durable", "lesson", "an old lesson that will be superseded first")
        new = self.seed("durable", "lesson", "the newer lesson that supersedes the old one")
        self.assertEqual(self.mem("supersede", old, "--by", new).returncode, 0)
        ok = self.seed("durable", "lesson", "a normal project lesson that may be reinforced")
        before = self.rows()
        targets = [pending, profile, global_id, other, old, "no-such-id"]
        actions = [{"kind": "reinforce", "target_id": t} for t in targets]
        actions += [{"kind": "supersede", "old_id": t, "body": f"replacement text for {t}"} for t in targets]
        actions.append({"kind": "reinforce", "target_id": ok})
        result, batch = self.apply(actions)
        self.assertEqual(result.returncode, 0, result.stderr)
        doc = self.result_json(batch)
        got = {(r["kind"], r["reason"]) for r in doc["rejected"]}
        for reason in ("pending-protected", "profile-protected", "global-protected", "other-project",
                       "inactive", "nonexistent"):
            self.assertIn(("reinforce", reason), got)
            self.assertIn(("supersede", reason), got)
        self.assertEqual(len(doc["rejected"]), 12)
        self.assertEqual(doc["reinforced"], [ok])
        after = self.rows()
        for rid in targets[:4] + [old]:
            self.assertEqual(after[rid], before[rid], rid)
        self.assertEqual(after[ok][1], before[ok][1] + 1)

    def test_scope_type_body_and_source_rules(self):
        result, batch = self.apply([
            self.add_action(1, scope="global"),
            self.add_action(2, type="profile"),
            self.add_action(3, type="handoff"),
            self.add_action(4, tier="forever"),
            {"kind": "add", "type": "lesson", "tier": "working", "body": "x" * 2000},
            self.add_action(6, source_key="user-choice:abc"),
            self.add_action(7, source_key="same"), self.add_action(8, source_key="same"),
            {"kind": "supersede", "old_id": "x", "body": "a body", "new_ref": "r"},
        ])
        self.assertEqual(result.returncode, 0, result.stderr)
        reasons = [r["reason"] for r in self.result_json(batch)["rejected"]]
        self.assertEqual(reasons, ["scope-not-project", "type-invalid", "type-invalid", "tier-invalid",
                                   "body-too-large", "source-reserved", "duplicate-source",
                                   "supersede-needs-body-xor-new-ref"])

    def test_snapshot_allowlist_parity(self):
        from tidy_apply import _target_problem
        durable = self.seed("durable", "lesson", "a durable project lesson for the allowlist")
        working = self.seed("working", "thread", "a working project thread for the allowlist")
        pending = self.seed("working", "handoff", "pending handoff kept out of the allowlist", "--requires-consume")
        profile = self.seed("durable", "profile", "profile line kept out of the allowlist entirely")
        other = self.seed("durable", "lesson", "another project lesson kept out of the allowlist", "--cwd-origin", OTHER_PROJECT)
        global_id = self.seed("durable", "lesson", "a global lesson kept out of the allowlist", "--scope", "global")
        snap = self.mem("curate-snapshot")
        self.assertEqual(snap.returncode, 0, snap.stderr)
        ids = set(re.search(r"^IDS: (.*)$", snap.stdout, re.M).group(1).split())
        with self.iso.patched_environ():
            mem = load_fresh_mem()
            pkey = mem.project_key(self.proj)
            con = mem.get_con()
            try:
                verdict = {rid: _target_problem(mem, con, rid, pkey) == ""
                           for rid in (durable, working, pending, profile, other, global_id)}
            finally:
                con.close()
        # The snapshot lists every durable/working id; tidy also fences out profile records,
        # exactly as the curator commands' own project gate does.
        self.assertEqual({rid for rid, ok in verdict.items() if ok}, (ids & set(verdict)) - {profile})
        self.assertEqual({rid for rid, ok in verdict.items() if ok}, {durable, working})


class ObligationOwnershipTest(TidyApplyCase):
    def test_tidy_uses_the_obligation_owner_and_steward_labels_only_its_target(self):
        import session_tidy as st
        import session_tidy_runner as runner
        import steward_check as check
        from fleet.collectors import steward
        from memory_open_work import owner_link

        own = {"harness": "claude", "session_id": "captain"}
        target = {"harness": "opencode", "session_id": "helper"}
        foreign = {"harness": "codex", "session_id": "helper"}
        foreign_project = self.iso.root / "other"
        foreign_project.mkdir()
        for repo in (self.proj, foreign_project):
            for command in (["git", "init", "-q"],
                            ["git", "remote", "add", "origin", f"https://example.invalid/team/{repo.name}"]):
                result = self.iso.run(command, cwd=repo)
                self.assertEqual(result.returncode, 0, result.stderr)
        rows = [SimpleNamespace(**owner, cwd=str(repo)) for owner, repo in
                ((own, self.proj), (target, self.proj), (foreign, foreign_project))]
        marker = {"session_id": "captain", "targets": {
            "local": dict(target, source="start", name="Sparrow", ts="1"),
            "foreign": dict(foreign, source="watch", name="Wren", ts="2")}}
        legacy = "Old project plan mentions Sparrow"
        legacy_id = self.seed("working", "unresolved-obligation", legacy)
        foreign_legacy = "Older promise made by Wren"
        self.seed("working", "unresolved-obligation", foreign_legacy,
                  "--links", owner_link(("codex", "helper")))
        legacy_rows = self.rows()

        transcript = self.work / "captain.jsonl"
        transcript.write_text(json.dumps({"type": "user", "message": {"role": "user", "content":
            "I will check the sample. Sparrow agreed to trace the fixture. "
            "Quoted from Wren in the other project: I will package the release. "
            "The note says there is another follow-up, but gives no owner."}}) + "\n", encoding="utf-8")
        with self.iso.patched_environ(), \
                mock.patch.object(steward, "_registry_sessions", return_value=rows), \
                mock.patch.object(steward, "read_markers", return_value={("claude", "captain"): marker}), \
                mock.patch.object(steward, "read_relations", return_value=[]):
            seat = st.seat_for_project("claude", st.project_key_for(self.proj))
            item = {"seat": seat.as_dict(), "harness": "claude", "sid": "captain",
                    "cwd": str(self.proj), "transcript": str(transcript)}
            run_dir = st.ensure_dir(self.state / "runs" / "obligation-input")
            bundle = runner.assemble(item, "obligation-input", run_dir)
            data = json.loads(bundle.path.read_text(encoding="utf-8"))
            self.assertEqual(data["obligation_owners"], [own, target])
            self.assertEqual([row["sid"] for row in data["sessions"]], ["captain"])
            self.assertIn("Quoted from Wren", data["sessions"][0]["text"])
            result, batch = self.apply([
                self.add_action(1, type="unresolved-obligation", body="Check the new sample", owner=own),
                self.add_action(2, type="unresolved-obligation", body="Trace the fixture", owner=target),
                self.add_action(3, type="unresolved-obligation", body="Package the other release", owner=foreign),
                self.add_action(4, type="unresolved-obligation", body="Follow up with an unknown owner"),
                self.add_action(5, type="observation", body="[미해결] Reference to the packaging note"),
                self.add_action(6, type="lesson", body="A follow-up described in an open headline",
                                headline="[미해결] Unattributed repeat check"),
                {"kind": "supersede", "old_id": legacy_id,
                 "body": "Rewrite an older obligation without confirming its owner"},
            ], input_doc=data)
            self.assertEqual(result.returncode, 0, result.stderr)
            report = self.result_json(batch)
            self.assertEqual(len(report["added"]), 3)
            self.assertEqual([row["reason"] for row in report["skipped"]],
                             ["obligation-owner-unconfirmed"] * 4)
            with mock.patch.object(check, "_end_observation", return_value=None), \
                    mock.patch.object(check, "_session_liveness", return_value="live"), \
                    mock.patch.object(check, "_pending_count", return_value=0), \
                    mock.patch.object(check, "_observe_lab", return_value={}), \
                    mock.patch("peer_steward_native.native_agent",
                               return_value={"result": {"agent": {"agent_status": "idle"}}}):
                seen = check._observe_role("claude", "captain")

        self.assertEqual([row["name"] for row in seen["targets"]], ["Sparrow"])
        self.assertEqual(check._classify(seen), "assign-next")
        self.assertCountEqual(check.next_actions(seen), [
            "[Sparrow] waiting · Trace the fixture → assign now",
            "open: Check the new sample → assign or do now",
            f"open: {legacy} → assign or do now",
        ])
        final_rows = self.rows()
        self.assertEqual({rid: final_rows[rid] for rid in legacy_rows}, legacy_rows)
        self.assertIn(("observation", "[미해결] Reference to the packaging note"),
                      {(row[6], row[5]) for row in final_rows.values()})


class StewardObligationContinuityTest(TidyApplyCase):
    def setUp(self):
        super().setUp()
        import steward_check as check
        import session_tidy_runner as runner
        import session_tidy as tidy
        from fleet.collectors import steward

        self.check, self.runner, self.tidy, self.steward = check, runner, tidy, steward
        self.own = {"harness": "claude", "session_id": "captain"}
        self.prior = {"harness": "claude", "session_id": "earlier-captain"}
        self.sparrow = {"harness": "opencode", "session_id": "sparrow"}
        self.old_sparrow = dict(self.sparrow, session_id="old-sparrow")
        self.kite = {"harness": "codex", "session_id": "kite"}
        self.finch = {"harness": "claude", "session_id": "finch"}
        self.wren = {"harness": "codex", "session_id": "sparrow"}
        self.raven = {"harness": "claude", "session_id": "raven"}
        self.foreign = self.iso.root / "other"
        self.foreign.mkdir()
        for repo in (self.proj, self.foreign):
            for command in (["git", "init", "-q"],
                            ["git", "remote", "add", "origin", f"https://example.invalid/team/{repo.name}"]):
                result = self.iso.run(command, cwd=repo)
                self.assertEqual(result.returncode, 0, result.stderr)
        self.census = [SimpleNamespace(**identity, cwd=str(repo)) for identity, repo in
                       ((self.own, self.proj), (self.sparrow, self.proj), (self.kite, self.proj),
                        (self.wren, self.foreign), (self.raven, self.foreign))]
        self.census[1].session_aliases = ["old-sparrow"]
        self.relation = {
            "origin": {"harness": "claude", "sid": "earlier-captain"},
            "holder": {"harness": "claude", "sid": "captain"},
            "lineage": [{"from": "earlier-captain", "to": "captain", "snapshot_id": "mock-clear"}],
            "targets": [dict(owner, name=name, source="watch") for owner, name in
                        ((self.old_sparrow, "Sparrow"), (self.kite, "Kite"), (self.finch, "Finch"),
                         (self.raven, "Raven"))],
        }
        self.relations = [self.relation]
        self.relation_failure = None
        self.states = {}

    @contextlib.contextmanager
    def project(self):
        with self.iso.patched_environ(), contextlib.ExitStack() as stack:
            for patch in (
                mock.patch.object(self.steward, "_registry_sessions", return_value=self.census),
                mock.patch.object(self.steward, "read_markers", return_value={}),
                mock.patch.object(self.steward, "read_relations", return_value=self.relations,
                                  side_effect=self.relation_failure),
                mock.patch.object(self.check, "_end_observation", return_value=None),
                mock.patch.object(self.check, "_pending_count", return_value=0),
                mock.patch.object(self.check, "_observe_lab", return_value={}),
                mock.patch.object(self.check, "_session_liveness",
                                  side_effect=lambda h, s, *a: "ended" if s == "finch" else "live"),
                mock.patch("peer_steward_native.native_agent", side_effect=lambda h, s: {
                    "result": {"agent": {"agent_status": self.states.get(s, "idle")}}}),
            ):
                stack.enter_context(patch)
            yield

    def promise(self, body, *owners):
        from memory_open_work import owner_link
        return self.seed("working", "unresolved-obligation", body, "--links", ",".join(
            owner_link((owner["harness"], owner["session_id"])) for owner in owners))

    def report_actions(self):
        with self.project():
            seen = self.check._observe_role("claude", "captain")
            return self.check.next_actions(seen)

    def bundle(self, seat, transcript):
        run = self.tidy.ensure_dir(self.state / "runs" / "ownership-scene")
        item = {"seat": seat.as_dict(), "harness": "claude", "sid": "captain",
                "cwd": str(self.proj), "transcript": str(transcript)}
        bundle = self.runner.assemble(item, "ownership-scene", run)
        return json.loads(bundle.path.read_text(encoding="utf-8"))

    def transcript(self, sid, text):
        path = self.work / f"{sid}.jsonl"
        path.write_text(json.dumps({"type": "user", "message": {"role": "user", "content": text}}) + "\n")
        return path

    def test_handover_keeps_the_holders_prior_own_work(self):
        self.promise("Read the earlier sample", self.prior)
        self.promise("Read the new sample", self.own)
        self.promise("Foreign update still pending", self.wren)
        own_work = ["open: Read the earlier sample → assign or do now",
                    "open: Read the new sample → assign or do now"]
        self.assertCountEqual(self.report_actions(), own_work)
        self.relation["released"] = True
        self.assertCountEqual(self.report_actions(), own_work)

    def test_unresolved_keeps_existing_work_until_the_relation_is_released(self):
        self.promise("Finch still owes the field notes", self.finch)
        self.promise("Finish the other repository task", self.raven)
        self.assertEqual(self.report_actions(), ["open: Finch still owes the field notes → assign or do now"])
        self.relation["released"] = True
        self.assertEqual(self.report_actions(), [])

    def test_same_pane_predecessor_tail_can_write_its_own_obligation_without_a_role(self):
        import time

        self.relations = []
        tidy = self.tidy
        seat = tidy._pane_seat_of("fixture:pane")
        current = self.transcript("captain", "The samples already came in.")
        old = self.transcript("earlier-captain", "I owe the measurements for the samples.")
        with self.project():
            for sid, path, source, at in ((self.prior["session_id"], old, "startup", time.time() - 5),
                                          (self.own["session_id"], current, "clear", time.time())):
                tidy._append_ledger_entry(seat, {"event": "start", "harness": "claude", "sid": sid,
                    "source": source, "cwd": str(self.proj), "transcript": str(path), "ts": at})
            tidy._append_ledger_entry(seat, {"event": "handover", "harness": "claude",
                "sid": "captain", "from": "earlier-captain", "source": "clear",
                "cwd": str(self.proj), "ts": time.time(), "bindings": []})
            data = self.bundle(seat, current)
            self.assertIn("I owe the measurements", " ".join(row["text"] for row in data["sessions"]))
            result, batch = self.apply([
                self.add_action(1, type="unresolved-obligation", body="Check the legacy measurements",
                                owner=self.prior),
                self.add_action(2, type="unresolved-obligation", body="Separate project packaging",
                                owner=self.wren),
            ], input_doc=data)
        self.assertEqual(result.returncode, 0, result.stderr)
        doc = self.result_json(batch)
        self.assertEqual(len(doc["added"]), 1)
        self.assertEqual(len(doc["skipped"]), 1)
        # The same preserved origin is visible to a check after role handover.
        self.relations = [self.relation]
        self.assertEqual(self.report_actions(), ["open: Check the legacy measurements → assign or do now"])

    def test_unreadable_roles_keep_existing_work_as_unsigned_project_work(self):
        self.promise("Trace the unfinished fixture", self.old_sparrow)
        self.promise("Foreign release already entered", self.wren)
        self.relation_failure = OSError("fixture ledger unreadable")
        self.assertEqual(self.report_actions(), ["open: Trace the unfinished fixture → assign or do now"])

    def test_owner_scope_scene_table(self):
        legacy = self.seed("working", "unresolved-obligation", "Old project work remains")
        conflicting = self.promise("Two different owners are named", self.sparrow, self.kite)
        before = self.rows()
        with self.project():
            current = self.transcript("captain", "I will prepare the sample. Sparrow and Kite have next steps.")
            data = self.bundle(self.tidy.seat_for_project("claude", self.tidy.project_key_for(self.proj)), current)
            proposals = [
                ("Prepare the new sample", self.own),
                ("Sparrow will trace the fixture", self.old_sparrow),
                ("Kite will check the table", self.kite),
                ("Wren will bundle the other release", self.wren),
                ("Raven will check the other project", self.raven),
                ("Someone will follow up", None),
                ("Either session might follow up", [self.sparrow, self.kite]),
            ]
            actions = [self.add_action(i, type="unresolved-obligation", body=body, owner=owner)
                       for i, (body, owner) in enumerate(proposals)]
            actions.append(self.add_action(20, type="observation", body="Useful reference from Wren"))
            result, batch = self.apply(actions, input_doc=data)
            self.assertEqual(result.returncode, 0, result.stderr)
        doc = self.result_json(batch)
        self.assertEqual(len(doc["added"]), 4)
        self.assertEqual([row["reason"] for row in doc["skipped"]], ["obligation-owner-unconfirmed"] * 4)
        after = self.rows()
        self.assertEqual({key: after[key] for key in (legacy, conflicting)}, before)
        self.assertIn(("observation", "Useful reference from Wren"), {(row[6], row[5]) for row in after.values()})
        generic = ["open: Prepare the new sample → assign or do now",
                   "open: Old project work remains → assign or do now",
                   "open: Two different owners are named → assign or do now"]
        sparrow = "[Sparrow] waiting · Sparrow will trace the fixture → assign now"
        self.assertCountEqual(self.report_actions(), [*generic, sparrow,
            "[Kite] waiting · Kite will check the table → assign now"])
        self.states["kite"] = "working"
        self.assertCountEqual(self.report_actions(), [*generic, sparrow,
            "open: Kite will check the table → assign or do now"])
        self.relation["released"] = True
        self.assertCountEqual(self.report_actions(), generic)


class BudgetTest(TidyApplyCase):

    def count(self, rtype):
        return sum(1 for row in self.rows().values() if row[6] == rtype)

    def test_model_proposals_are_capped_at_ten_new_records(self):
        result, batch = self.apply([self.add_action(n) for n in range(15)])
        self.assertEqual(result.returncode, 0, result.stderr)
        doc = self.result_json(batch)
        self.assertEqual((len(doc["added"]), doc["discarded_over_budget"]), (10, 5))
        self.assertEqual(self.count("lesson"), 10)
        self.assertIn("추가 10", self.last_line(result))
        self.assertIn("상한 초과 폐기 5", self.last_line(result))

    def test_choices_use_the_budget_first_then_the_model_gets_the_rest(self):
        result, batch = self.apply([self.add_action(n) for n in range(9)],
                                   input_doc={"user_choices": [self.choice(n) for n in range(3)]})
        self.assertEqual(result.returncode, 0, result.stderr)
        doc = self.result_json(batch)
        self.assertEqual((len(doc["choice_added"]), len(doc["added"]), doc["discarded_over_budget"]), (3, 7, 2))
        self.assertEqual((self.count("decision"), self.count("lesson")), (3, 7))
        for row in self.rows().values():
            if row[6] == "decision":
                self.assertEqual(row[7], "working")

    def test_choices_alone_may_exceed_ten_and_are_all_recorded(self):
        result, batch = self.apply([self.add_action(n) for n in range(4)],
                                   input_doc={"user_choices": [self.choice(n) for n in range(12)]})
        self.assertEqual(result.returncode, 0, result.stderr)
        doc = self.result_json(batch)
        self.assertEqual((len(doc["choice_added"]), len(doc["added"]), doc["choice_over_cap"]), (12, 0, 2))
        self.assertEqual(doc["discarded_over_budget"], 4)
        self.assertEqual(self.count("decision"), 12)
        self.assertIn("선택지 기록이 상한 10건을 2건 넘어 모두 기록", self.last_line(result))

    def test_duplicate_groups_are_limited_to_five(self):
        ids = [self.seed("durable", "lesson", f"lesson number {n} for the duplicate group test") for n in range(7)]
        result, batch = self.apply([{"kind": "reinforce", "target_id": rid, "duplicate_group": f"g{n}"}
                                    for n, rid in enumerate(ids)])
        self.assertEqual(result.returncode, 0, result.stderr)
        doc = self.result_json(batch)
        self.assertEqual(len(doc["reinforced"]), 5)
        self.assertEqual([r["reason"] for r in doc["rejected"]], ["duplicate-group-limit"] * 2)

    def test_input_digest_mismatch_refuses_model_actions_but_keeps_the_choices(self):
        result, batch = self.apply([self.add_action(1)], input_doc={"user_choices": [self.choice(1)]},
                                   input_digest="0" * 64)
        self.assertEqual(result.returncode, 0, result.stderr)
        doc = self.result_json(batch)
        self.assertEqual((len(doc["choice_added"]), len(doc["added"])), (1, 0))
        self.assertEqual([r["reason"] for r in doc["rejected"]], ["input-digest-mismatch"])


class SummaryAndIdempotenceTest(TidyApplyCase):

    def test_the_last_line_is_one_summary_with_the_undo_command(self):
        old = self.seed("durable", "lesson", "an old lesson to be replaced in the summary test")
        hot = self.seed("durable", "lesson", "a lesson that keeps coming back in practice")
        result, batch = self.apply([
            self.add_action(1), self.add_action(2),
            {"kind": "supersede", "old_id": old, "body": "the replacement for the old lesson text"},
            {"kind": "reinforce", "target_id": hot},
            {"kind": "reinforce", "target_id": "gone"},
        ])
        self.assertEqual(result.returncode, 0, result.stderr)
        line = self.last_line(result)
        match = SUMMARY_RE.match(line)
        self.assertTrue(match, line)
        self.assertEqual(match.group(1), match.group(2))
        self.assertIn("추가 2 · 갱신 1 · 강화 1 · 건너뜀 0 · 거부 1", line)
        self.assertEqual(self.result_json(batch)["summary"], line)

    def test_the_same_batch_twice_reports_the_first_result_and_changes_nothing(self):
        path, batch = self.actions([self.add_action(1)], batch="same-batch")
        first = self.mem("tidy-apply", str(path), "--cwd", str(self.proj))
        rows = self.rows()
        second = self.mem("tidy-apply", str(path), "--cwd", str(self.proj))
        self.assertEqual((first.returncode, second.returncode), (0, 0))
        self.assertEqual(self.last_line(first), self.last_line(second))
        self.assertEqual(self.rows(), rows)

    def test_the_same_actions_under_a_new_batch_add_no_duplicates(self):
        old = self.seed("durable", "lesson", "an old lesson replaced exactly once only")
        actions = [self.add_action(1), self.add_action(2),
                   {"kind": "supersede", "old_id": old, "body": "its replacement that is added once"}]
        self.apply(actions, batch="one")
        count = len(self.rows())
        result, batch = self.apply(actions, batch="two")
        self.assertEqual(result.returncode, 0, result.stderr)
        doc = self.result_json(batch)
        self.assertEqual(doc["added"], [])
        self.assertEqual(len(self.rows()), count)
        self.assertEqual({(s["kind"], s["reason"]) for s in doc["skipped"]}, {("add", "source-exists")})
        self.assertEqual([r["reason"] for r in doc["rejected"]], ["inactive"])

    def test_bad_input_is_one_stderr_line_and_exit_two(self):
        bad = self.work / "bad.json"
        bad.write_text("{not json", encoding="utf-8")
        result = self.mem("tidy-apply", str(bad), "--cwd", str(self.proj))
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertEqual(len(result.stderr.strip().splitlines()), 1)


class UndoTest(TidyApplyCase):

    def test_apply_then_undo_returns_the_old_records_and_deletes_nothing(self):
        old = self.seed("durable", "lesson", "an old lesson that the batch will replace")
        hot = self.seed("durable", "lesson", "a lesson that the batch will reinforce")
        before = self.rows()
        result, batch = self.apply([
            self.add_action(1), self.add_action(2),
            {"kind": "supersede", "old_id": old, "body": "the replacement lesson written by the batch"},
            {"kind": "reinforce", "target_id": hot},
        ])
        self.assertEqual(result.returncode, 0, result.stderr)
        applied = self.rows()
        self.assertEqual(applied[old][0], "superseded")
        self.assertEqual(applied[hot][1], before[hot][1] + 1)
        undo = self.mem("tidy-undo", batch)
        self.assertEqual(undo.returncode, 0, undo.stderr + undo.stdout)
        after = self.rows()
        for rid in (old, hot):
            self.assertEqual(after[rid], before[rid], rid)
        self.assertTrue(set(applied) <= set(after), "undo must not delete a record")
        new_ids = set(after) - set(before)
        self.assertEqual(len(new_ids), 4)   # two adds, one replacement, one undo marker
        self.assertEqual([after[rid][6] for rid in new_ids if after[rid][0] == "active"], ["tidy-undo"])
        self.assertIn("삭제 없이 되돌렸습니다", self.last_line(undo))
        again = self.mem("tidy-undo", batch)
        self.assertEqual(again.returncode, 0)
        self.assertIn("이미 되돌렸습니다", self.last_line(again))

    def test_undo_refuses_when_a_later_change_touched_a_new_record(self):
        result, batch = self.apply([self.add_action(1)])
        added = self.result_json(batch)["added"][0]
        self.assertEqual(self.mem("reinforce", added).returncode, 0)
        before = self.rows()
        undo = self.mem("tidy-undo", batch)
        self.assertEqual(undo.returncode, 1)
        self.assertIn("되돌리기를 거부했습니다", self.last_line(undo))
        self.assertIn(added, self.last_line(undo))
        self.assertEqual(self.rows(), before)

    def test_lines_read_in_english_without_a_korean_declaration(self):
        # The suites pin Korean (tidy_isolation); the catalog default is English.
        english = {"FLEET_NOW_LANG": "English"}
        old = self.seed("durable", "lesson", "an old lesson the english batch replaces")
        path, batch = self.actions([self.add_action(1), self.add_action(2),
                                    {"kind": "supersede", "old_id": old, "body": "its english replacement"},
                                    {"kind": "reinforce", "target_id": "gone"}])
        result = self.mem("tidy-apply", str(path), "--cwd", str(self.proj), extra=english)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.last_line(result),
                         f"[tidy] batch {batch}: added 2 · updated 1 · reinforced 0 · skipped 0 · rejected 1"
                         f" — undo: mem tidy-undo {batch}")
        added = self.result_json(batch)["added"][0]
        self.assertEqual(self.mem("reinforce", added).returncode, 0)
        refused = self.mem("tidy-undo", batch, extra=english)
        self.assertEqual(refused.returncode, 1)
        self.assertEqual(self.last_line(refused), f"[tidy] batch {batch}: undo refused — "
                                                  f"record {added} changed after the batch was applied")
        _, fresh = self.apply([self.add_action(3)])
        undo = self.mem("tidy-undo", fresh, extra=english)
        self.assertEqual(self.last_line(undo), f"[tidy] batch {fresh}: 1 undone, nothing deleted")
        again = self.mem("tidy-undo", fresh, extra=english)
        self.assertEqual(self.last_line(again), f"[tidy] batch {fresh}: already undone")

    def test_undo_of_unknown_batch_is_a_one_line_error(self):
        undo = self.mem("tidy-undo", "nope")
        self.assertEqual(undo.returncode, 2)
        self.assertEqual(len(undo.stderr.strip().splitlines()), 1)

    def test_a_failure_halfway_stops_as_partial_and_undo_still_restores(self):
        old = self.seed("durable", "lesson", "an old lesson before the failing batch runs")
        hot = self.seed("durable", "lesson", "a lesson reinforced before the failing batch")
        before = self.rows()
        path, batch = self.actions([
            {"kind": "reinforce", "target_id": hot},
            self.add_action(1),
            {"kind": "supersede", "old_id": old, "body": "this write is made to fail by the test"},
            self.add_action(3),
        ], batch="halfway")
        with self.iso.patched_environ():
            mem = load_fresh_mem("mem_partial")
            import tidy_apply
            real = mem.write_record
            calls = []

            def flaky(*args, **kwargs):
                calls.append(1)
                if len(calls) == 2:
                    raise RuntimeError("injected write failure")
                return real(*args, **kwargs)

            mem.write_record = flaky
            args = type("A", (), {"actions": str(path), "input": None, "cwd": str(self.proj)})()
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = tidy_apply.apply_command(mem, args)
            self.assertEqual(code, 1)
            line = out.getvalue().strip().splitlines()[-1]
            self.assertIn("부분 적용 2건", line)
            self.assertIn("RuntimeError", line)
            self.assertIn(f"되돌리기: mem tidy-undo {batch}", line)
            doc = self.result_json(batch)
            self.assertEqual(doc["status"], "partial")
            self.assertEqual((len(doc["reinforced"]), len(doc["added"])), (1, 1))
            mem.write_record = real
            os.environ.pop("MEM_ACTOR", None)
        undo = self.mem("tidy-undo", batch)
        self.assertEqual(undo.returncode, 0, undo.stderr + undo.stdout)
        after = self.rows()
        for rid in (old, hot):
            self.assertEqual(after[rid], before[rid], rid)
        self.assertEqual([after[rid][6] for rid in set(after) - set(before) if after[rid][0] == "active"],
                         ["tidy-undo"])
        # A batch that stopped halfway is never replayed (a reinforce must not run twice).
        replay = self.mem("tidy-apply", str(path), "--cwd", str(self.proj))
        self.assertEqual(self.rows(), after)
        self.assertEqual(replay.returncode, 0 if self.result_json(batch)["status"] == "applied" else 1)


class UnconfirmedWriteTest(TidyApplyCase):
    """The store write lands, then the journal's ``done`` flush fails: the step stays ``intent``."""

    def apply_with_failing_done(self, kind, actions, batch):
        path, _ = self.actions(actions, batch=batch)
        with self.iso.patched_environ():
            mem = load_fresh_mem("mem_unconfirmed")
            import tidy_apply
            real_done = tidy_apply.Journal.done

            def flaky(journal, op, **fields):
                if op["op"] == kind:
                    raise OSError("journal flush failed")
                return real_done(journal, op, **fields)

            tidy_apply.Journal.done = flaky
            try:
                args = type("A", (), {"actions": str(path), "input": None, "cwd": str(self.proj)})()
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(tidy_apply.apply_command(mem, args), 1)
            finally:
                tidy_apply.Journal.done = real_done
                os.environ.pop("MEM_ACTOR", None)
        return json.loads((self.state / "runs" / batch / "undo.json").read_text(encoding="utf-8"))

    def assert_reported_with_undo(self, batch, expect_done):
        import session_tidy_runner as runner
        applied = runner.applied_writes(self.state / "runs" / batch)
        self.assertEqual(applied, (expect_done, 1))
        with self.iso.patched_environ():          # the suite's Korean declaration (tidy_isolation)
            line = runner.failure_line("apply stopped: OSError: journal flush failed", batch, applied)
        self.assertNotIn("그대로", line)
        self.assertIn(f"되돌리기: mem tidy-undo {batch}", line)

    def undo(self, batch):
        undo = self.mem("tidy-undo", batch)
        self.assertEqual(undo.returncode, 0, undo.stderr + undo.stdout)
        return self.rows()

    def test_an_add_left_as_intent_is_reported_and_undone(self):
        before = self.rows()
        journal = self.apply_with_failing_done("add", [self.add_action(1)], "u-add")
        self.assertEqual([op["state"] for op in journal["ops"]], ["intent"])
        landed = set(self.rows()) - set(before)
        self.assertEqual(len(landed), 1)
        self.assert_reported_with_undo("u-add", 0)
        after = self.undo("u-add")
        self.assertNotEqual(after[landed.pop()][0], "active")

    def test_a_supersede_left_as_intent_is_reported_and_undone(self):
        old = self.seed("durable", "lesson", "an old lesson whose supersede is not confirmed")
        before = self.rows()
        journal = self.apply_with_failing_done(
            "supersede", [{"kind": "supersede", "old_id": old, "body": "the replacement written before the flush failed"}],
            "u-sup")
        self.assertEqual([(op["op"], op["state"]) for op in journal["ops"]], [("add", "done"), ("supersede", "intent")])
        self.assertEqual(self.rows()[old][0], "superseded")
        self.assert_reported_with_undo("u-sup", 1)
        after = self.undo("u-sup")
        self.assertEqual(after[old], before[old])
        self.assertEqual([after[rid][6] for rid in set(after) - set(before) if after[rid][0] == "active"],
                         ["tidy-undo"])

    def test_a_reinforce_left_as_intent_is_reported_and_undone(self):
        hot = self.seed("durable", "lesson", "a lesson whose reinforce is not confirmed")
        before = self.rows()
        journal = self.apply_with_failing_done("reinforce", [{"kind": "reinforce", "target_id": hot}], "u-rein")
        self.assertEqual([op["state"] for op in journal["ops"]], ["intent"])
        self.assertEqual(self.rows()[hot][1], before[hot][1] + 1)
        self.assert_reported_with_undo("u-rein", 0)
        self.assertEqual(self.undo("u-rein")[hot], before[hot])


class WaitingDecisionTest(TidyApplyCase):

    def test_waiting_decisions_are_written_before_the_model_and_the_file_is_removed(self):
        import tidy_decisions as td
        with self.iso.patched_environ():
            payload = td.make_payload("나중에 반영할 질문입니까?", ["예"], options=[{"label": "예", "description": ""}],
                                      origin={"kind": "frame-interview", "route_id": "rt-x", "round": 1},
                                      cwd=str(self.proj))
            path = td.save_pending(payload)
        self.assertTrue(path and path.exists())
        self.assertEqual(oct(path.stat().st_mode & 0o777), "0o600")
        result, batch = self.apply([self.add_action(1)])
        self.assertEqual(result.returncode, 0, result.stderr)
        doc = self.result_json(batch)
        self.assertEqual((len(doc["choice_added"]), len(doc["added"])), (1, 1))
        self.assertFalse(path.exists())
        decisions = [row for row in self.rows().values() if row[6] == "decision"]
        self.assertEqual(len(decisions), 1)
        self.assertIn("나중에 반영할 질문입니까?", decisions[0][5])
        # the same decision waiting again is skipped, not written twice
        with self.iso.patched_environ():
            td.save_pending(payload)
        self.apply([])
        self.assertEqual(len([r for r in self.rows().values() if r[6] == "decision"]), 1)


if __name__ == "__main__":
    unittest.main()
