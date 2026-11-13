import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import contextlib
import hashlib
import importlib.util
import io
import json
import os
import re
import shutil
import stat
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import artifact_meta as M
import artifact_producer as P
import artifact_workflow_group_review as R
import campaign_title_repair as repair


class CampaignTitleRepairTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "root"
        campaign = self.root / "campaigns" / "2026-09-01_demo"
        cycle = campaign / "2026-09-01_demo"
        cycle.mkdir(parents=True)
        (campaign / "campaign.json").write_text(json.dumps({
            "campaign_id": "camp_demo", "title": "Old title", "cycles": ["cyc_demo"],
        }), encoding="utf-8")
        os.chmod(campaign / "campaign.json", 0o644)
        self.manifest = cycle / "manifest.json"
        self.manifest.write_text("  \n" + json.dumps({
            "manifest_id": "man_demo", "manifest_revision_id": "mrev_demo",
            "campaign": {"campaign_id": "camp_demo", "title": "Old title"},
            "cycle": {"cycle_id": "cyc_demo"},
        }, indent=2) + "\n", encoding="utf-8")
        self.roots = Path(self.tmp.name) / "roots.json"
        self.roots.write_text(json.dumps({"roots": [{
            "artifact_root_id": "root_demo", "artifact_root_path": str(self.root),
        }]}), encoding="utf-8")
        self.proposals = Path(self.tmp.name) / "proposals.json"
        self.proposals.write_text(json.dumps({"entries": [{
            "artifact_root_id": "root_demo", "campaign_locator": "2026-09-01_demo",
            "display_title": "읽기 좋은 제목",
        }]}), encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def test_apply_writes_sidecar_and_preserves_manifest_bytes(self):
        before = self.manifest.read_bytes()
        package = repair.prepare(self.roots, self.proposals)
        self.assertNotEqual(package["entries"][0]["manifest_digests"][0], repair.digest_bytes(before))
        self.assertEqual(package["entries"][0]["manifest_digests"][0], repair.digest_json(json.loads(before)))
        self.assertEqual(package["entries"][0]["manifest_bindings"], [{
            "manifest_revision_id": "mrev_demo",
            "manifest_digest": repair.digest_json(json.loads(before)),
        }])
        result = repair.apply(package)
        self.assertEqual(result["campaigns"], 1)
        self.assertEqual(json.loads((self.root / "campaigns/2026-09-01_demo/campaign.json").read_text())["title"], "읽기 좋은 제목")
        self.assertEqual(stat.S_IMODE(os.stat(self.root / "campaigns/2026-09-01_demo/campaign.json").st_mode), 0o644)
        declaration = json.loads((self.root / repair.DISPLAY_TITLE_REL).read_text())
        self.assertEqual(declaration["schema"], repair.DECLARATION_SCHEMA)
        self.assertEqual(declaration["entries"][0]["display_title"], "읽기 좋은 제목")
        self.assertEqual(self.manifest.read_bytes(), before)
        self.assertEqual(repair.verify(package)["status"], "verified")

    def test_partial_apply_and_rollback_preserve_other_declarations(self):
        path = self.root / repair.DISPLAY_TITLE_REL
        path.parent.mkdir(parents=True, exist_ok=True)
        other = {"campaign_id": "camp_other", "campaign_locator": "other", "display_title": "Kept",
                 "manifest_bindings": [], "manifest_revision_ids": [], "manifest_digests": []}
        path.write_text(json.dumps({"schema": repair.DECLARATION_SCHEMA, "artifact_root_id": "root_demo",
                                    "entries": [other], "custom": "preserved"}))
        package = repair.prepare(self.roots, self.proposals)
        repair.apply(package)
        result = json.loads(path.read_text())
        self.assertIn(other, result["entries"])
        self.assertEqual(result["custom"], "preserved")
        self.assertEqual(repair.verify(package)["status"], "verified")
        repair.rollback(package)
        self.assertEqual(json.loads(path.read_text())["entries"], [other])

    def test_open_campaign_without_sealed_manifest_can_apply(self):
        self.manifest.unlink()
        package = repair.prepare(self.roots, self.proposals)
        self.assertEqual(package["entries"][0]["manifest_bindings"], [])
        self.assertEqual(repair.apply(package)["campaigns_changed"], 1)
        self.assertEqual(repair.verify(package)["status"], "verified")
        self.assertFalse(self.manifest.exists())

    def test_prepare_refuses_unlisted_campaign(self):
        self.proposals.write_text(json.dumps({"entries": []}), encoding="utf-8")
        with self.assertRaisesRegex(repair.RepairError, "proposal-missing"):
            repair.prepare(self.roots, self.proposals)

    def test_title_quality_rejects_duplicate_forbidden_and_long(self):
        with self.assertRaisesRegex(repair.RepairError, "display-title-duplicate"):
            repair.validate_titles([
                {"campaign_locator": "a", "display_title": "같은 제목"},
                {"campaign_locator": "b", "display_title": "같은 제목"},
            ])
        with self.assertRaisesRegex(repair.RepairError, "display-title-forbidden"):
            repair.validate_titles([{"campaign_locator": "a", "display_title": "legacy project material"}])
        with self.assertRaisesRegex(repair.RepairError, "display-title-too-long"):
            repair.validate_titles([{"campaign_locator": "a", "display_title": "가" * 35}])

    def test_manifest_bindings_sort_pairs_not_independent_arrays(self):
        rows = [
            {"manifest_revision_id": "mrev_z", "manifest_digest": "sha256:00" + "0" * 62},
            {"manifest_revision_id": "mrev_a", "manifest_digest": "sha256:ff" + "f" * 62},
        ]
        bindings, revisions, digests = repair.manifest_binding_fields(rows)
        self.assertEqual([row["manifest_revision_id"] for row in bindings], ["mrev_a", "mrev_z"])
        self.assertEqual(revisions, ["mrev_a", "mrev_z"])
        self.assertEqual(digests, ["sha256:ff" + "f" * 62, "sha256:00" + "0" * 62])
        self.assertNotEqual(sorted(revisions), sorted(digests))

    def test_extra_manifest_is_allowed_but_missing_anchor_is_rejected(self):
        package = repair.prepare(self.roots, self.proposals)
        extra = self.manifest.parent / "later-cycle"
        extra.mkdir()
        (extra / "manifest.json").write_text(json.dumps({
            "manifest_id": "man_later", "manifest_revision_id": "mrev_later",
            "campaign": {"campaign_id": "camp_demo", "title": "Old title"},
            "cycle": {"cycle_id": "cyc_later"},
        }), encoding="utf-8")
        missing = json.loads(json.dumps(package))
        missing["entries"][0]["manifest_bindings"] = [{
            "manifest_revision_id": "mrev_missing",
            "manifest_digest": "sha256:" + "a" * 64,
        }]
        missing["entries"][0]["manifest_revision_ids"] = ["mrev_missing"]
        missing["entries"][0]["manifest_digests"] = ["sha256:" + "a" * 64]
        with self.assertRaisesRegex(repair.RepairError, "manifest-binding-drift"):
            repair.apply(missing)
        repair.apply(package)
        self.assertEqual(repair.verify(package)["status"], "verified")

    def test_nested_payload_manifest_is_not_discovered_as_cycle_control(self):
        payload = self.manifest.parent / "artifacts" / "_internal" / "candidate"
        payload.mkdir(parents=True)
        (payload / "manifest.json").write_text(json.dumps({
            "manifest_id": "man_spoof", "manifest_revision_id": "mrev_spoof",
            "campaign": {"campaign_id": "camp_demo", "title": "Spoof"},
            "cycle": {"cycle_id": "cyc_demo"},
        }), encoding="utf-8")
        rows = repair.manifest_rows(self.manifest.parents[1])
        self.assertEqual([row["manifest_id"] for row in rows], ["man_demo"])

    def test_transaction_failure_restores_pre_apply_bytes_separately_from_full_rollback(self):
        package = repair.prepare(self.roots, self.proposals)
        package["transaction_journal_path"] = str(Path(self.tmp.name) / "failure-journal.json")
        original_write_atomic = repair.write_atomic

        def fail_sidecar(path, value):
            if Path(path).name == "campaign-display-titles.json":
                raise RuntimeError("injected sidecar publish failure")
            return original_write_atomic(path, value)

        with patch.object(repair, "write_atomic", side_effect=fail_sidecar):
            with self.assertRaisesRegex(RuntimeError, "injected sidecar publish failure"):
                repair.apply(package)

        campaign_json = self.root / "campaigns/2026-09-01_demo/campaign.json"
        self.assertEqual(json.loads(campaign_json.read_text())['title'], "Old title")
        self.assertFalse((self.root / repair.DISPLAY_TITLE_REL).exists())
        self.assertEqual(repair.read_json(Path(package["transaction_journal_path"]))["state"], "rolled-back")

    def test_review_and_promote_preserve_current_state_without_writing_campaigns(self):
        package = repair.prepare(self.roots, self.proposals)
        repair.apply(package)
        applied = Path(self.tmp.name) / "applied.json"
        package["entries"][0]["original_title"] = "Earliest title"
        applied.write_text(json.dumps(package, ensure_ascii=False), encoding="utf-8")
        review_path = Path(self.tmp.name) / "review.json"
        review = repair.review_snapshot(self.roots, self.proposals, applied)
        repair.write_atomic(review_path, review)
        self.assertEqual(review["schema"], repair.REVIEW_SCHEMA)
        self.assertEqual(review["entries"][0]["original_title"], "Earliest title")
        self.assertEqual(review["entries"][0]["current_title"], "읽기 좋은 제목")
        promoted_path = Path(self.tmp.name) / "promoted.json"
        result = repair.promote(review_path, promoted_path, "APPROVE hearting-campaign-title-review/v1")
        self.assertEqual(result["campaigns"], 1)
        promoted = repair.read_json(promoted_path)
        self.assertEqual(promoted["entries"][0]["old_campaign_title"], "읽기 좋은 제목")
        self.assertEqual(json.loads((self.root / "campaigns/2026-09-01_demo/campaign.json").read_text())["title"], "읽기 좋은 제목")
        promoted["transaction_journal_path"] = str(Path(self.tmp.name) / "journal.json")
        repair.apply(promoted)
        self.assertEqual(repair.read_json(Path(self.tmp.name) / "journal.json")["state"], "committed")
        rollback_result = repair.rollback(promoted)
        self.assertEqual(rollback_result["rollback_kind"], "full-original")
        self.assertEqual(json.loads((self.root / "campaigns/2026-09-01_demo/campaign.json").read_text())["title"], "Earliest title")
        self.assertFalse((self.root / repair.DISPLAY_TITLE_REL).exists())

    def test_review_scopes_to_applied_package_and_records_new_unapproved_campaign(self):
        package = repair.prepare(self.roots, self.proposals)
        repair.apply(package)
        extra = self.root / "campaigns" / "2026-09-13_new_campaign"
        extra.mkdir()
        (extra / "campaign.json").write_text(json.dumps({"campaign_id": "camp_extra", "title": "unapproved"}), encoding="utf-8")
        applied = Path(self.tmp.name) / "applied.json"
        applied.write_text(json.dumps(package, ensure_ascii=False), encoding="utf-8")
        review = repair.review_snapshot(self.roots, self.proposals, applied)
        self.assertEqual(len(review["entries"]), 1)
        self.assertEqual(review["unreviewed_campaigns"][0]["campaign_id"], "camp_extra")


_SPEC = importlib.util.spec_from_file_location(
    "campaign_title_repair_producer_fixture", Path(__file__).with_name("artifact_producer.test.py"))
fixture = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(fixture)

PAST = 1_700_000_000.0
NOW = 1_790_000_000.0
START = "=== CAMPAIGN DATA ===\n"
END = "\n=== END DATA ==="


def data_of(prompt):
    return json.loads(prompt.split(START, 1)[1].rsplit(END, 1)[0])


def unified(title, summary="한 일과 결과를 한 줄로 적음", harness="claude", **over):
    """An `invoke` that records prompts and answers the unified v2 reply (groups: none; one title for
    the campaign); the model is never called."""
    calls = []

    def invoke(prompt):
        calls.append(prompt)
        data = data_of(prompt)
        vocab = [item["code"] for item in data["project_meta"]["branches"]]
        entity = {"title": title, "summary": summary, "branches": [vocab[0] if vocab else "GEN"], "kinds": ["평가"]}
        reply = {"decisions": [{"cycle_id": cid, "verdict": "none", "reason": "근거 한 문장"}
                               for cid in data["group_target_ids"]],
                 "new_groups": [], "relations": [],
                 "metadata": {"campaign": dict(entity), "cycles": {cid: dict(entity, title=f"{title} 사이클")
                                                                     for cid in data["metadata_target_ids"]}},
                 "new_branches": [] if vocab else [{"code": "GEN", "label": "일반", "note": "기본 갈래"}]}
        reply.update(over)
        return json.dumps(reply, ensure_ascii=False), harness
    invoke.calls = calls
    return invoke


def raw(text, harness="claude"):
    return lambda prompt: (text, harness)


def tree_snapshot(root):
    rows = []
    for path in sorted(Path(root).rglob("*")):
        meta = path.lstat()
        digest = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() and not path.is_symlink() else ""
        rows.append((str(path.relative_to(root)), meta.st_size, meta.st_mtime_ns, digest))
    return rows


class AutoBase(fixture.ProducerTestBase):
    def setUp(self):
        self._attempt = os.environ.pop("AGENT_DISPATCH_ATTEMPT_ID", None)
        self.addCleanup(self._restore_attempt)
        super().setUp()
        env = patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        for name in (repair.AUTO_DISABLE_ENV, R.DISABLE_ENV):
            os.environ.pop(name, None)
        self.activate()

    def _restore_attempt(self):
        if self._attempt is not None:
            os.environ["AGENT_DISPATCH_ATTEMPT_ID"] = self._attempt

    # -- fixtures -------------------------------------------------------
    def seal(self, key="camp", slug="cycle", now=None):
        route, route_file = self.route(slug=slug, campaign_key=key)
        begun = P.begin(self.root, route_file=route_file, capability="autopilot-code",
                        intensity="direct", campaign_key=key, now=now)
        self.write_output(begun, "reports/final_report.md", b"# Body\n\nwork for " + slug.encode() + b"\n")
        self.close(route, route_file)
        P.finalize(self.root, cycle_id=begun["cycle_id"], primary="reports/final_report.md", now=now)
        return begun

    def ready(self, key="camp", slug="cycle"):
        route, route_file = self.route(slug=slug, campaign_key=key)
        begun = P.begin(self.root, route_file=route_file, capability="autopilot-code",
                        intensity="direct", campaign_key=key)
        self.write_output(begun, "reports/final_report.md", b"# Body\n\nwork\n")
        self.close(route, route_file)
        return begun

    def finalize(self, begun):
        P.finalize(self.root, cycle_id=begun["cycle_id"], primary="reports/final_report.md")

    def cdir(self, begun):
        for directory in repair.artifact_locator.iter_campaign_dirs(self.root):
            if repair.read_json(directory / "campaign.json").get("campaign_id") == begun["campaign_id"]:
                return directory
        raise AssertionError("campaign directory not found")

    def set_campaign_title(self, cdir, title):
        record = repair.read_json(cdir / "campaign.json")
        record["title"] = title
        repair.write_atomic(cdir / "campaign.json", record)

    @property
    def declaration_path(self):
        return self.root / repair.DISPLAY_TITLE_REL

    def declaration(self):
        return json.loads(self.declaration_path.read_text(encoding="utf-8"))

    def entry(self, cdir, title):
        record = repair.read_json(cdir / "campaign.json")
        rows = repair.manifest_rows(cdir)
        bindings, revisions, digests = repair.manifest_binding_fields(rows)
        return {"campaign_id": record["campaign_id"], "campaign_locator": cdir.name, "display_title": title,
                "manifest_bindings": bindings, "manifest_revision_ids": revisions, "manifest_digests": digests}

    def write_declaration(self, entries, **top):
        doc = {"schema": repair.DECLARATION_SCHEMA, "artifact_root_id": fixture.ROOT_ID,
               "entries": sorted(entries, key=lambda row: row["campaign_id"]), **top}
        repair.write_atomic(self.declaration_path, doc)
        return doc

    def meta(self, begun):
        read = M.read_campaign_meta(self.root, begun["campaign_id"])
        return read.doc if read.status == "ok" else None

    def record(self):
        status, doc = R.read_record(self.root)
        self.assertEqual(status, "ok")
        return doc

    def human_apply(self, cdir, title):
        roots = Path(self._tmp.name) / "human-roots.json"
        roots.write_text(json.dumps({"roots": [{"artifact_root_id": fixture.ROOT_ID,
                                                "artifact_root_path": str(self.root)}]}), encoding="utf-8")
        proposals = Path(self._tmp.name) / "human-proposals.json"
        proposals.write_text(json.dumps({"entries": [{
            "artifact_root_id": fixture.ROOT_ID, "campaign_locator": cdir.name, "display_title": title}]},
            ensure_ascii=False), encoding="utf-8")
        package = repair.prepare(roots, proposals, allow_unlisted=True)
        return package, repair.apply(package)

    def auto(self, begun, invoke, **kw):
        return repair.auto_title(self.root, campaign_ids=[begun["campaign_id"]], mode="seal", invoke=invoke, **kw)


class AutoSelectionAndWriteTest(AutoBase):
    def test_untitled_campaign_seal_writes_the_title_to_meta_json_in_one_call(self):  # A1
        begun = self.seal(key="ax-train", slug="train")
        cdir = self.cdir(begun)
        campaign_before = (cdir / "campaign.json").read_bytes()
        manifests_before = {path: path.read_bytes() for path in cdir.rglob("manifest.json")}
        invoke = unified("AX 명령어 모델 학습")
        result = self.auto(begun, invoke)
        self.assertEqual(result["status"], "ok")
        self.assertEqual([row["status"] for row in result["targets"]], ["written"])
        self.assertEqual(result["targets"][0]["display_title"], "AX 명령어 모델 학습")
        self.assertEqual(len(invoke.calls), 1)  # one judgement decides groups, titles, summaries, and tags
        doc = self.meta(begun)
        self.assertEqual((doc["campaign"]["title"], doc["campaign"]["source"]["title"]["by"]), ("AX 명령어 모델 학습", "model"))
        self.assertEqual((doc["campaign"]["short_id"], doc["campaign"]["branches"]), ("GEN-01", ["GEN"]))
        self.assertEqual(doc["cycles"][begun["cycle_id"]]["title"], "AX 명령어 모델 학습 사이클")
        self.assertFalse(self.declaration_path.exists())  # the old declaration is never written
        self.assertEqual((cdir / "campaign.json").read_bytes(), campaign_before)
        self.assertEqual({path: path.read_bytes() for path in cdir.rglob("manifest.json")}, manifests_before)
        self.assertTrue(any(e["field"] == "campaign.title" for e in M.H.iter_events(self.root)))
        self.assertEqual(data_of(invoke.calls[0])["campaign"]["campaign_id"], begun["campaign_id"])

    def test_declaration_titles_are_a_persons_and_stay(self):  # A2, A3, A4
        titled = self.seal(key="human-a", slug="a")
        ascii_title = self.seal(key="asciiproj", slug="b")
        slug_equal = self.seal(key="slug-eq", slug="c")
        record_only = self.seal(key="human-d", slug="d")
        self.write_declaration([self.entry(self.cdir(titled), "사람이 정한 한글 제목"),
                                self.entry(self.cdir(ascii_title), "Human Chosen Name"),
                                self.entry(self.cdir(slug_equal), "slug-eq")], custom="preserved")
        self.set_campaign_title(self.cdir(record_only), "기록에 있는 한글 제목")
        before = self.declaration_path.read_bytes()
        campaign_json = (self.cdir(record_only) / "campaign.json").read_bytes()
        invoke = unified("자동 제목이다")
        result = repair.auto_title(self.root, mode="backfill", invoke=invoke)
        self.assertEqual(len(invoke.calls), 4)
        by_id = {row["campaign_id"]: row for row in result["targets"]}
        for begun, title in ((titled, "사람이 정한 한글 제목"), (ascii_title, "Human Chosen Name"), (slug_equal, "slug-eq")):
            doc = self.meta(begun)
            self.assertEqual((doc["campaign"]["title"], doc["campaign"]["source"]["title"]["by"]), (title, "human"))
            self.assertEqual(by_id[begun["campaign_id"]]["status"], "skipped")
            self.assertEqual(doc["campaign"]["summary"], "한 일과 결과를 한 줄로 적음")  # only the title is held
        self.assertEqual(self.meta(record_only)["campaign"]["title"], "자동 제목이다")  # campaign.json is no declaration
        self.assertEqual(by_id[record_only["campaign_id"]]["status"], "written")
        self.assertEqual(self.declaration_path.read_bytes(), before)
        self.assertEqual((self.cdir(record_only) / "campaign.json").read_bytes(), campaign_json)
        # a person can hand any of them back to the model, and the next run fills it
        M.run_write(self.root, lambda ws: M.op_release(ws, titled["campaign_id"], None, ["title"]), now=NOW)
        repair.auto_title(self.root, mode="explicit", campaign_ids=[titled["campaign_id"]], invoke=unified("다시 맡긴 제목"))
        self.assertEqual(self.meta(titled)["campaign"]["title"], "사람이 정한 한글 제목")  # filled only by a new judgement
        self.seal(key="human-a", slug="a2")  # the next seal's judgement refills the released field
        self.auto(titled, unified("다시 맡긴 제목"))
        self.assertEqual(self.meta(titled)["campaign"]["title"], "다시 맡긴 제목")
        self.assertEqual(self.declaration_path.read_bytes(), before)

    def test_model_failure_and_trigger_failures_keep_the_seal_and_write_nothing(self):  # A5
        begun = self.seal(key="fails", slug="a")
        before = {row[0]: row[3] for row in tree_snapshot(self.root) if "workflow-group-review" not in row[0] and row[3]}
        result = self.auto(begun, raw(""))
        self.assertEqual((result["targets"][0]["status"], result["targets"][0]["failure_class"]), ("failed", "unavailable"))
        after = {row[0]: row[3] for row in tree_snapshot(self.root) if "workflow-group-review" not in row[0] and row[3]}
        self.assertEqual(after, before)
        self.assertIsNone(self.meta(begun))
        self.assertEqual(self.record()["cycles"][begun["cycle_id"]]["failure_class"], "unavailable")
        raising = self.ready(key="hook", slug="hook-raises")
        with patch.object(R, "launch_after_seal", side_effect=RuntimeError("boom")):
            self.finalize(raising)
        self.assertEqual(P.read_cycle_record(self.root, raising["cycle_id"])["state"], "sealed")
        spawning = self.ready(key="spawn", slug="spawn-fails")
        with patch.object(R, "in_test_process", return_value=False), \
                patch.object(R.subprocess, "Popen", side_effect=OSError("no fork")) as popen:
            self.finalize(spawning)
        self.assertTrue(any("--auto" in call.args[0] for call in popen.call_args_list))
        self.assertEqual(P.read_cycle_record(self.root, spawning["cycle_id"])["state"], "sealed")
        self.assertTrue(list(self.cdir(spawning).rglob("manifest.json")))

    def test_invalid_responses_are_rejected_and_the_title_rules_are_the_public_limits(self):  # A6
        begun = self.seal(key="invalid", slug="a")
        entity = lambda **o: {"title": "정상 제목", "summary": "요약", "branches": ["GEN"], "kinds": ["평가"], **o}  # noqa: E731

        def reply(title="정상 제목", extra=None):
            def invoke(prompt):
                data = data_of(prompt)
                vocab = [item["code"] for item in data["project_meta"]["branches"]]
                body = {"decisions": [{"cycle_id": c, "verdict": "none", "reason": "r"} for c in data["group_target_ids"]],
                        "new_groups": [], "relations": [],
                        "metadata": {"campaign": entity(title=title, branches=[vocab[0] if vocab else "GEN"]),
                                     "cycles": {c: entity(branches=[vocab[0] if vocab else "GEN"])
                                                for c in data["metadata_target_ids"]}},
                        "new_branches": [] if vocab else [{"code": "GEN", "label": "일반", "note": ""}]}
                body.update(extra or {})
                return json.dumps(body, ensure_ascii=False), "claude"
            return invoke

        rejected = {"too-long": reply("가" * 121), "empty": reply("  "), "control": reply("가나\x01다라"),
                    "extra-key": reply(extra={"display_title": "x"}), "parse": raw("not json at all")}
        for code, invoke in rejected.items():
            with self.subTest(code):
                self.seal(key="invalid", slug=f"bad-{code}")  # a fresh cycle: failed ones stop after three hard failures
                result = self.auto(begun, invoke)
                self.assertEqual((result["targets"][0]["status"], result["targets"][0]["failure_class"]),
                                 ("failed", "invalid-response"))
                self.assertIsNone(self.meta(begun))
                self.assertFalse(self.declaration_path.exists())
        # the old 34-character, Hangul-minimum, and generic-label rules are not copied: the model judges meaning
        for ok, title in (("long", "가" * 120), ("ascii", "Train Model v8"), ("generic", "요약")):
            with self.subTest(ok):
                self.seal(key="invalid", slug=f"again-{ok}")
                result = self.auto(begun, reply(title))
                self.assertEqual(result["targets"][0]["status"], "written", result)
                self.assertEqual(self.meta(begun)["campaign"]["title"], title)
        with self.subTest("fenced"):
            self.seal(key="invalid", slug="again-fenced")

            def fenced(prompt):
                text, harness = reply("펜스로 감싼 정상 제목")(prompt)
                return "```json\n" + text + "\n```\n끝.", harness

            self.assertEqual(self.auto(begun, fenced)["targets"][0]["status"], "written")

    def test_failure_is_retried_at_next_seal(self):  # A7
        begun = self.seal(key="retry", slug="first", now=PAST)
        self.assertEqual(self.auto(begun, raw(""))["targets"][0]["status"], "failed")
        self.seal(key="retry", slug="second", now=PAST + 100)
        result = self.auto(begun, unified("다시 시도해서 얻은 제목"))
        self.assertEqual(result["targets"][0]["status"], "written")
        self.assertEqual(self.meta(begun)["campaign"]["title"], "다시 시도해서 얻은 제목")
        self.assertEqual({self.record()["cycles"][cid]["verdict"] for cid in self.meta(begun)["cycles"]}, {"unassigned"})

    def test_a_later_sealed_cycle_gets_its_own_metadata_and_refreshes_the_campaign_summary(self):  # A8
        begun = self.seal(key="later", slug="first", now=PAST)
        self.assertEqual(self.auto(begun, unified("처음 봉인에서 얻은 제목", "첫 요약"))["targets"][0]["status"], "written")
        declaration_exists = self.declaration_path.exists()
        second = self.seal(key="later", slug="second", now=PAST + 100)
        invoke = unified("두 번째 제목이다", "갱신한 요약")
        result = self.auto(second, invoke)
        self.assertEqual(len(invoke.calls), 1)
        self.assertEqual(data_of(invoke.calls[0])["metadata_target_ids"], [second["cycle_id"]])  # the first cycle is not re-sent
        doc = self.meta(second)
        self.assertEqual(set(doc["cycles"]), {begun["cycle_id"], second["cycle_id"]})
        self.assertEqual((doc["campaign"]["summary"], doc["campaign"]["title"]), ("갱신한 요약", "두 번째 제목이다"))
        self.assertEqual(result["targets"][0]["status"], "written")
        self.assertEqual(self.declaration_path.exists(), declaration_exists)  # still no declaration written
        self.assertEqual(doc["cycles"][begun["cycle_id"]]["title"], "처음 봉인에서 얻은 제목 사이클")

    def test_a_campaign_without_a_sealed_cycle_is_not_sent(self):  # A9
        route, route_file = self.route(slug="open", campaign_key="open-camp")
        P.begin(self.root, route_file=route_file, capability="autopilot-code", intensity="direct",
                campaign_key="open-camp")
        invoke = unified("열린 캠페인 제목")
        result = repair.auto_title(self.root, mode="explicit", invoke=invoke)
        self.assertEqual((invoke.calls, result["targets"]), ([], []))

    def test_an_unreadable_or_foreign_declaration_is_kept_and_protects_the_title(self):  # A10
        begun = self.seal(key="unreadable", slug="a")
        for label, payload in (("broken", b"{not json"),
                               ("other-root", json.dumps({"schema": repair.DECLARATION_SCHEMA,
                                                          "artifact_root_id": "root_" + "9" * 32,
                                                          "entries": []}).encode())):
            with self.subTest(label):
                self.declaration_path.write_bytes(payload)
                self.seal(key="unreadable", slug=f"again-{label}")
                invoke = unified("덮어쓰면 안 되는 제목")
                result = self.auto(begun, invoke)
                self.assertEqual(len(invoke.calls), 1)  # the rest of the judgement is not held back
                self.assertEqual(result["targets"][0]["status"], "skipped")
                self.assertNotIn("title", self.meta(begun)["campaign"])
                self.assertEqual(self.meta(begun)["campaign"]["summary"], "한 일과 결과를 한 줄로 적음")
                self.assertEqual(self.declaration_path.read_bytes(), payload)

    def test_dry_run_writes_nothing(self):  # A11
        begun = self.seal(key="dry", slug="a")
        before = tree_snapshot(self.root)
        result = self.auto(begun, unified("미리 보는 제목이다"), dry_run=True)
        self.assertEqual(result["status"], "dry-run")
        self.assertEqual((result["targets"][0]["status"], result["targets"][0]["display_title"]),
                         ("proposed", "미리 보는 제목이다"))
        failed = self.auto(begun, raw(""), dry_run=True)
        self.assertEqual(failed["targets"][0]["failure_class"], "unavailable")
        self.assertEqual(tree_snapshot(self.root), before)

    def test_busy_review_lock_returns_busy_and_leaves_a_pending_marker(self):  # A14
        begun = self.seal(key="busy", slug="a")
        held = R._try_flock(self.root)
        self.assertIsNotNone(held)
        try:
            invoke = unified("바쁠 때 제목")
            result = self.auto(begun, invoke)
        finally:
            R._unlock(held)
        self.assertEqual((result["status"], result["targets"], invoke.calls), ("busy", [], []))
        self.assertEqual(result["queued"], [begun["campaign_id"]])
        self.assertEqual(R._pending_ids(self.root), [begun["cycle_id"]])
        self.assertEqual(self.auto(begun, unified("이제는 쓰는 제목"))["targets"][0]["status"], "written")
        self.assertEqual(R._pending_ids(self.root), [])

    def test_campaign_sealed_while_another_run_holds_the_lock_is_judged_by_that_run(self):  # A14b
        first = self.seal(key="race-first", slug="a")
        paused, resume = threading.Event(), threading.Event()
        seen = []

        def invoke(prompt):
            seen.append(data_of(prompt)["campaign"]["campaign_id"])
            if len(seen) == 1:
                paused.set()  # the first run has selected its targets and holds the lock
                self.assertTrue(resume.wait(30))
            return unified(f"{len(seen)}번째 캠페인 제목")(prompt)

        outcome = {}
        worker = threading.Thread(target=lambda: outcome.update(repair.auto_title(
            self.root, campaign_ids=[first["campaign_id"]], mode="seal", invoke=invoke)))
        worker.start()
        try:
            self.assertTrue(paused.wait(30))
            second = self.seal(key="race-second", slug="b")
            busy = self.auto(second, unified("호출되면 안 되는 제목"))
            self.assertEqual((busy["status"], busy["targets"]), ("busy", []))
            self.assertEqual(R._pending_ids(self.root), [second["cycle_id"]])
        finally:
            resume.set()
            worker.join(60)
        self.assertFalse(worker.is_alive())
        self.assertEqual(outcome["status"], "ok")
        self.assertEqual([row["campaign_id"] for row in outcome["targets"]], [first["campaign_id"], second["campaign_id"]])
        self.assertEqual([row["status"] for row in outcome["targets"]], ["written", "written"])
        self.assertEqual(R._pending_ids(self.root), [])
        self.assertEqual({self.record()["cycles"][c]["mode"] for c in (first["cycle_id"], second["cycle_id"])}, {"auto"})

    def test_a_seal_queued_during_the_final_pass_waits_for_the_next_seal_without_a_second_job(self):
        first = self.seal(key="cap-first", slug="a")
        late = self.seal(key="cap-late", slug="b", now=PAST)  # sealed before enrollment: reached only as a pending trigger
        calls = []

        def invoke(prompt):
            calls.append(prompt)
            R._touch_pending(self.root, late["cycle_id"])  # queued while the final pass runs
            return unified("마지막 패스 제목")(prompt)

        with patch.object(R, "MAX_PASSES", 1), patch.object(R, "in_test_process", return_value=False), \
                patch.object(R.subprocess, "Popen") as popen:
            result = repair.auto_title(self.root, campaign_ids=[first["campaign_id"]], mode="seal", invoke=invoke)
        self.assertEqual((len(calls), [row["status"] for row in result["targets"]]), (1, ["written"]))
        popen.assert_not_called()  # no title-only follow-up child exists any more
        self.assertEqual(R._pending_ids(self.root), [late["cycle_id"]])  # kept for the next sweep
        repair.auto_title(self.root, campaign_ids=[late["campaign_id"]], mode="seal", invoke=unified("따라온 제목"))
        self.assertEqual(R._pending_ids(self.root), [])
        self.assertEqual(self.meta(late)["campaign"]["title"], "따라온 제목")

    def test_pending_marker_for_an_already_judged_cycle_is_cleared_and_junk_is_ignored(self):
        begun = self.seal(key="stale", slug="a")
        self.assertEqual(self.auto(begun, unified("이미 제목이 있는 캠페인"))["targets"][0]["status"], "written")
        R._touch_pending(self.root, begun["cycle_id"])
        R.pending_dir(self.root).joinpath("not-a-cycle").touch()
        self.assertEqual(R._pending_ids(self.root), [begun["cycle_id"]])
        invoke = unified("다시 쓰면 안 되는 제목")
        repair.auto_title(self.root, campaign_ids=[begun["campaign_id"]], mode="seal", invoke=invoke)
        self.assertEqual((invoke.calls, R._pending_ids(self.root)), ([], []))


class AutoTriggerAndCliTest(AutoBase):
    def spawned(self):
        return patch.object(R.subprocess, "Popen")

    def test_trigger_is_the_one_review_launch(self):  # A12
        begun = self.seal(key="trigger", slug="a")
        record = {"campaign_id": begun["campaign_id"], "cycle_id": begun["cycle_id"]}
        with self.spawned() as popen:
            self.assertFalse(repair.launch_after_seal(self.root, record))  # test process
        popen.assert_not_called()
        with patch.object(R, "in_test_process", return_value=False):
            with self.spawned() as popen:
                self.assertTrue(repair.launch_after_seal(self.root, record))
            popen.assert_called_once()
            argv = popen.call_args.args[0]
            self.assertEqual(argv[1], str(Path(R.__file__).resolve()))  # the review, never a title child
            self.assertEqual(argv[2:], ["sweep", "--artifact-root", str(self.root), "--auto", "--cycle", begun["cycle_id"]])
            self.assertTrue(popen.call_args.kwargs["start_new_session"])
            with self.spawned() as popen, patch.dict(os.environ, {R.DISABLE_ENV: "off"}):
                self.assertFalse(repair.launch_after_seal(self.root, record))  # the review switch stops it
            with self.spawned() as popen, patch.dict(os.environ, {repair.AUTO_DISABLE_ENV: "off"}):
                self.assertTrue(repair.launch_after_seal(self.root, record))  # the title switch does not
            with self.spawned() as popen:
                self.assertFalse(repair.launch_after_seal(self.root / "nowhere", record))
                self.assertFalse(repair.launch_after_seal(self.root, {"cycle_id": "not-a-cycle"}))
                self.assertFalse(repair.launch_after_seal(self.root, {}))
            popen.assert_not_called()

    def test_backfill_over_root_declaration(self):  # A13
        begun = self.seal(key="backfill", slug="a")
        copy = Path(self._tmp.name) / "copy-root"
        shutil.copytree(self.root, copy)
        roots = Path(self._tmp.name) / "roots.json"
        roots.write_text(json.dumps({"roots": [
            {"artifact_root_id": fixture.ROOT_ID, "artifact_root_path": str(self.root), "display_name": "main"},
            {"artifact_root_id": "root_" + "c" * 32, "artifact_root_path": str(copy)},
            {"artifact_root_id": "root_" + "d" * 32, "artifact_root_path": str(copy / "missing")},
        ]}), encoding="utf-8")
        result = repair.backfill_roots(roots, invoke=unified("소급 적용한 제목"))
        first, mismatch, missing = result["roots"]
        self.assertEqual((first["status"], first["targets"], first["written"], first["display_name"]), ("ok", 1, 1, "main"))
        self.assertEqual([(row["status"], row["code"]) for row in (mismatch, missing)],
                         [("skipped", "root-identity-mismatch")] * 2)
        self.assertEqual((result["totals"]["targets"], result["totals"]["written"], result["totals"]["failed"]), (1, 1, 0))
        self.assertIsNone(M.read_campaign_meta(copy, begun["campaign_id"]).doc)  # the mismatched root is untouched
        self.assertFalse((copy / repair.DISPLAY_TITLE_REL).exists())
        self.assertEqual(self.record()["cycles"][begun["cycle_id"]]["mode"], "explicit")
        again = repair.backfill_roots(roots, invoke=unified("두 번째 소급은 비어 있음"))
        self.assertEqual(again["totals"]["targets"], 0)  # a fill-in run skips what already has metadata

    def test_auto_backfill_and_the_review_sweep_are_one_job(self):
        begun = self.seal(key="one-job", slug="a")
        invoke = unified("같은 일을 하는 제목")
        with patch.object(R, "_invoke_model", side_effect=AssertionError("no second model path")):
            repair.auto_title(self.root, campaign_ids=[begun["campaign_id"]], mode="explicit", invoke=invoke)
        self.assertEqual(len(invoke.calls), 1)  # the wrapper adds no model call of its own
        self.assertEqual(self.meta(begun)["campaign"]["title"], "같은 일을 하는 제목")

    def test_cli_auto_and_backfill(self):  # A15
        begun = self.seal(key="cli", slug="a")

        def run(*argv):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = repair.main(list(argv))
            lines = out.getvalue().splitlines()
            self.assertEqual(len(lines), 1)
            return code, json.loads(lines[0])

        answer = unified("명령줄로 만든 제목")
        with patch.object(repair, "_default_invoke", side_effect=lambda prompt: answer(prompt)):
            code, result = run("auto", "--artifact-root", str(self.root), "--campaign", begun["campaign_id"], "--dry-run")
            self.assertEqual((code, result["status"], result["targets"][0]["status"]), (0, "dry-run", "proposed"))
            self.assertIsNone(self.meta(begun))
            roots = Path(self._tmp.name) / "cli-roots.json"
            roots.write_text(json.dumps({"roots": [{"artifact_root_id": fixture.ROOT_ID,
                                                    "artifact_root_path": str(self.root)}]}), encoding="utf-8")
            code, result = run("backfill", "--root-declaration", str(roots), "--dry-run")
            self.assertEqual((code, result["status"], result["totals"]["proposed"]), (0, "dry-run", 1))
            code, result = run("auto", "--artifact-root", str(self.root), "--mode", "explicit")
            self.assertEqual((code, result["targets"][0]["status"]), (0, "written"))
        with patch.object(repair, "_default_invoke", return_value=("", None)):
            other = self.seal(key="cli-fail", slug="b")
            code, result = run("auto", "--artifact-root", str(self.root), "--campaign", other["campaign_id"])
            self.assertEqual((code, result["targets"][0]["failure_class"]), (0, "unavailable"))
        self.assertEqual(run("auto", "--artifact-root", str(self.root / "nowhere"))[0], 65)
        self.assertEqual(run("auto", "--artifact-root", str(self.root), "--campaign", "bad")[1]["code"], "campaign-id-invalid")
        self.assertEqual(run("auto", "--artifact-root", str(self.root), "--limit", "0")[0], 65)
        self.assertEqual(run("backfill", "--root-declaration", str(self.root / "missing.json"))[0], 65)


class HumanRaceAndContractTest(AutoBase):
    def test_a_declaration_applied_during_the_model_call_is_a_persons_title_and_wins(self):  # A16
        begun = self.seal(key="race-a", slug="a")
        cdir = self.cdir(begun)

        def invoke(prompt):
            self.human_apply(cdir, "사람이 그사이 정한 제목")
            return unified("자동으로 만든 제목")(prompt)

        result = self.auto(begun, invoke)
        self.assertEqual((result["targets"][0]["status"], result["targets"][0]["code"]),
                         ("skipped", "title-protected-or-unchanged"))
        campaign = self.meta(begun)["campaign"]
        self.assertEqual((campaign["title"], campaign["source"]["title"]["by"]), ("사람이 그사이 정한 제목", "human"))
        (entry,) = self.declaration()["entries"]
        self.assertEqual(entry["display_title"], "사람이 그사이 정한 제목")  # the declaration stays as the person left it
        self.assertEqual(repair.read_json(cdir / "campaign.json")["title"], "사람이 그사이 정한 제목")

    def test_a_person_setting_the_meta_title_after_the_auto_write_wins_and_stays(self):  # A17
        first = self.seal(key="race-b", slug="a")
        self.assertEqual(self.auto(first, unified("자동으로 먼저 쓴 제목"))["targets"][0]["status"], "written")
        second = self.seal(key="race-c", slug="b")
        self.assertEqual(self.auto(second, unified("다른 캠페인 자동 제목"))["targets"][0]["status"], "written")
        M.run_write(self.root, lambda ws: M.op_set(ws, first["campaign_id"], None, {"title": "사람이 나중에 정한 제목"}),
                    now=NOW)
        self.assertEqual(self.meta(second)["campaign"]["title"], "다른 캠페인 자동 제목")
        self.seal(key="race-b", slug="c")
        invoke = unified("무시되어야 하는 제목")
        result = self.auto(first, invoke)
        self.assertEqual(self.meta(first)["campaign"]["title"], "사람이 나중에 정한 제목")
        self.assertEqual(result["targets"][0]["status"], "skipped")
        # the old repair tool still writes its declaration, but meta.json is the canonical title now
        self.human_apply(self.cdir(first), "옛 선언 도구로 정한 제목")
        self.assertEqual(self.meta(first)["campaign"]["title"], "사람이 나중에 정한 제목")
        self.assertEqual(M.effective_title(self.root, second["campaign_id"]), "다른 캠페인 자동 제목")

    def test_busy_declaration_lock_still_restores_the_manual_repair(self):  # A17 (manual repair only)
        begun = self.seal(key="race-d", slug="a")
        cdir = self.cdir(begun)
        roots = Path(self._tmp.name) / "busy-roots.json"
        roots.write_text(json.dumps({"roots": [{"artifact_root_id": fixture.ROOT_ID,
                                                "artifact_root_path": str(self.root)}]}), encoding="utf-8")
        proposals = Path(self._tmp.name) / "busy-proposals.json"
        proposals.write_text(json.dumps({"entries": [{"artifact_root_id": fixture.ROOT_ID,
                                                       "campaign_locator": cdir.name, "display_title": "사람 제목이다"}]},
                                        ensure_ascii=False), encoding="utf-8")
        package = repair.prepare(roots, proposals, allow_unlisted=True)
        package["transaction_journal_path"] = str(Path(self._tmp.name) / "journal.json")
        campaign_before = (cdir / "campaign.json").read_bytes()
        with repair._declaration_lock(self.root):
            with patch.object(repair, "REPAIR_LOCK_TIMEOUT", 0.05):
                with self.assertRaisesRegex(repair.RepairError, "declaration-lock-busy"):
                    repair.apply(package)
        self.assertEqual((cdir / "campaign.json").read_bytes(), campaign_before)
        self.assertFalse(self.declaration_path.exists())
        self.assertEqual(repair.read_json(Path(package["transaction_journal_path"]))["state"], "rolled-back")
        self.assertEqual(repair.apply(package)["status"], "applied")
        applied_declaration = self.declaration_path.read_bytes()
        with repair._declaration_lock(self.root), patch.object(repair, "REPAIR_LOCK_TIMEOUT", 0.05):
            with self.assertRaisesRegex(repair.RepairError, "declaration-lock-busy"):
                repair.rollback(package)
        self.assertEqual(self.declaration_path.read_bytes(), applied_declaration)
        self.assertEqual(repair.rollback(package)["status"], "rolled-back")

    def test_the_cairn_declaration_reader_port_is_kept_and_auto_leaves_declarations_alone(self):  # A18
        single = self.seal(key="c-single", slug="a", now=PAST)
        multi = self.seal(key="c-multi", slug="b", now=PAST + 10)
        self.seal(key="c-multi", slug="c", now=PAST + 20)
        target = self.seal(key="c-target", slug="d", now=PAST + 30)
        single_entry, multi_entry = self.entry(self.cdir(single), "바인딩 하나 제목"), self.entry(self.cdir(multi), "바인딩 여럿 제목")
        self.assertEqual((len(single_entry["manifest_bindings"]), len(multi_entry["manifest_bindings"])), (1, 2))
        for entry in (single_entry, multi_entry):
            self.assertTrue(all(re.fullmatch(r"mrev_[0-9a-f]{32}", rev) for rev in entry["manifest_revision_ids"]))
        self.write_declaration([single_entry, multi_entry])
        before = self.declaration_path.read_bytes()
        self.assertEqual(self.auto(target, unified("계약을 통과하는 제목"))["targets"][0]["status"], "written")
        self.assertEqual(self.declaration_path.read_bytes(), before)  # auto never edits the declaration now
        document = self.declaration()
        repair.check_cairn_declaration(document, fixture.ROOT_ID)
        self.assertEqual(len(document["entries"]), 2)

        def broken(mutate):
            copy = json.loads(json.dumps(document))
            mutate(next(row for row in copy["entries"] if row["campaign_id"] == multi["campaign_id"]))
            return copy

        negatives = {
            "bindings-empty": lambda e: e.update(manifest_bindings=[], manifest_revision_ids=[], manifest_digests=[]),
            "bindings-invalid": lambda e: e.update(manifest_bindings=e["manifest_bindings"][::-1],
                                                   manifest_revision_ids=e["manifest_revision_ids"][::-1],
                                                   manifest_digests=e["manifest_digests"][::-1]),
            "bindings-duplicate": lambda e: (e["manifest_bindings"][1].update(manifest_digest=e["manifest_bindings"][0]["manifest_digest"]),
                                             e.update(manifest_digests=[e["manifest_bindings"][0]["manifest_digest"]] * 2)),
            "binding-arrays-length": lambda e: e.update(manifest_digests=e["manifest_digests"][:1]),
            "binding-arrays-mismatch": lambda e: e.update(manifest_revision_ids=e["manifest_revision_ids"][::-1]),
            "campaign-id-duplicate": lambda e: e.update(campaign_id=single["campaign_id"]),
            "campaign-id-or-title-empty": lambda e: e.update(display_title="  "),
        }
        for code, mutate in negatives.items():
            with self.subTest(code):
                with self.assertRaises(repair.AutoTitleError) as ctx:
                    repair.check_cairn_declaration(broken(mutate), fixture.ROOT_ID)
                self.assertEqual(ctx.exception.code, f"cairn-contract:{code}")
        with self.subTest("digest-format"):
            def bad_digest(e):
                e["manifest_bindings"][0]["manifest_digest"] = "md5:abc"
            with self.assertRaises(repair.AutoTitleError) as ctx:
                repair.check_cairn_declaration(broken(bad_digest), fixture.ROOT_ID)
            self.assertEqual(ctx.exception.code, "cairn-contract:bindings-invalid")

    def test_membership_that_changes_during_the_call_is_refused_and_nothing_is_written(self):  # A19
        first = self.seal(key="anchor-a", slug="first", now=PAST)
        other = self.seal(key="anchor-b", slug="other", now=PAST + 5)

        def invoke(prompt):
            path = P.cycle_record_path(self.root, first["cycle_id"])
            record = json.loads(path.read_text(encoding="utf-8"))
            record["campaign_id"] = other["campaign_id"]  # the producer's membership moved after selection
            path.write_text(json.dumps(record), encoding="utf-8")
            return unified("소속이 바뀐 제목")(prompt)

        result = self.auto(first, invoke)
        self.assertEqual((result["targets"][0]["status"], result["targets"][0]["failure_class"], result["targets"][0]["code"]),
                         ("failed", "apply-failed", "cycle-not-member"))
        self.assertIsNone(self.meta(first))
        self.assertEqual([e for e in M.H.iter_events(self.root) if e["kind"] in ("meta", "group")  # the fixture's own close line is not the repair's
                      and e["target"]["id"] in (first["campaign_id"], first["cycle_id"])], [])


if __name__ == "__main__":
    unittest.main()
