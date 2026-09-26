#!/usr/bin/env python3
"""Campaign closure/reopen stream tests on isolated fixture roots."""
import concurrent.futures
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("AGENT_ARTIFACT_CHECKPOINT", "off")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import artifact_campaign as C
import artifact_producer as P
import artifact_manifest as M

spec_path = Path(__file__).with_name("artifact_producer.test.py")
import importlib.util
spec = importlib.util.spec_from_file_location("campaign_producer_fixture", spec_path)
F = importlib.util.module_from_spec(spec)
spec.loader.exec_module(F)


class CampaignTest(F.ProducerTestBase):
    def setUp(self):
        super().setUp()
        route, route_file, self.result = self.begin(campaign_key="campaign-closure")
        self.output = self.write_output(self.result)
        self.close(route, route_file)
        P.finalize(self.root, cycle_id=self.result["cycle_id"])
        self.campaign = self.result["campaign_id"]
        self.path = Path(self.result["cycle_dir"]).parent / "campaign.json"
        self.manifest_path = Path(self.result["cycle_dir"]) / "manifest.json"
        self.manifest_bytes = self.manifest_path.read_bytes()

    def _begin(self, *, key=None, campaign=None, parent=None, slug="reopen-cycle"):
        route = F.compile_for("direct", self.root, slug=slug, gate_source=slug)
        binding = F.L.admit_runtime_route(self.root, route)
        return P.begin(self.root, route_file=Path(binding.route_file), capability="autopilot-code",
                       intensity="direct", campaign_key=key,
                       campaign_id=campaign, parent_cycle_id=parent)

    def _close(self, reason="completion criterion met"):
        return C.close(self.root, self.path, reason=reason)

    def _write_v1_close(self, path=None):
        path = Path(path or self.path)
        record = json.loads(path.read_text())
        snapshot = C._snapshot(self.root, path)
        harness, session = "codex", "legacy-session"
        actor_id = "native-user:" + C.hashlib.sha256((harness + ":" + session).encode()).hexdigest()
        approval = {"harness": harness, "session_id": session, "actor_id": actor_id,
                    "statement": "campaign-" + "satisfy " + record["campaign_id"] + " " + C.digest(snapshot),
                    "decision": "accepted", "native_message_digest": "sha256:" + "a" * 64}
        first = snapshot["cycles"][0]
        event = {"stream_id": "strm_" + "1" * 32, "stream_sequence": 1,
                 "event_type": "campaign.satisfied", "target_id": record["campaign_id"],
                 "actor": {"kind": "user", "id": actor_id}, "recorded_at": "2026-09-26T00:00:00Z",
                 "provenance": {"source_manifest_id": first["manifest_id"],
                                "source_revision_id": first["manifest_revision_id"],
                                "producer_route_id": first["route_id"], "schema_version": 1,
                                "algorithm_version": C.CONTRACT_V1, "source_digest": C.digest(snapshot)},
                 "evidence_ids": [],
                 "payload": {"contract": C.CONTRACT_V1, "root": str(self.root),
                             "snapshot": snapshot, "approval": approval}}
        event["event_id"] = C._id("evt", event)
        raw = C.canonical(event) + b"\n"
        (path.parent / C.LEGACY_EVENT_NAME).write_bytes(raw)
        return raw

    def test_a24_1_agent_closes_with_reason_and_v2_event(self):
        with self.assertRaisesRegex(C.CampaignError, "campaign-close-reason-required"):
            C.close(self.root, self.path)
        result = self._close("the planned output is complete")
        self.assertEqual(result["status"], "satisfied")
        event = json.loads((self.path.parent / "campaign.events/000001.json").read_text())
        self.assertEqual(event["event_type"], "campaign.satisfied")
        self.assertEqual(event["actor"]["kind"], "producer")
        self.assertEqual(event["payload"]["closure"]["reason"], "the planned output is complete")
        self.assertEqual(json.loads(self.path.read_text())["state"], "satisfied")
        self.assertEqual(C.status(self.root, self.path)["state"], "satisfied")

    def test_status_reports_reason_need_commands_and_event_history(self):
        report = C.status(self.root, self.path)
        self.assertTrue(report["closable"])
        self.assertTrue(report["reason_required"])
        self.assertIn("--reason", report["close_command"])
        self._close()
        report = C.status(self.root, self.path)
        self.assertTrue(report["satisfied"])
        self.assertEqual(len(report["events"]), 1)
        self.assertTrue(report["reopen_command"])

    def test_same_key_multiple_closed_campaigns_is_ambiguous(self):
        rows = [{"key": "same", "state": "satisfied", "campaign_id": "camp_" + "1" * 32},
                {"key": "same", "state": "satisfied", "campaign_id": "camp_" + "2" * 32}]
        self.assertEqual(P.classify_campaign_key(rows, "same"),
                         {"mode": "blocked", "code": "campaign-key-reopen-ambiguous"})

    def test_a24_2_close_keeps_snapshot_readiness_gates(self):
        route = F.compile_for("direct", self.root, slug="open-cycle", gate_source="open")
        binding = F.L.admit_runtime_route(self.root, route)
        open_cycle = P.begin(self.root, route_file=Path(binding.route_file), capability="autopilot-code",
                             intensity="direct", campaign_id=self.campaign)
        with self.assertRaisesRegex(C.CampaignError, "campaign-cycle-not-sealed"):
            C.close(self.root, self.path, reason="done")
        self.assertEqual(C.campaign_state(self.root, self.path).state, "active")
        self.assertTrue(Path(open_cycle["cycle_dir"]).exists())

    def test_a24_3_key_id_and_parent_begin_reopen_same_campaign(self):
        selectors = ({"key": "campaign-closure"}, {"campaign": self.campaign},
                     {"parent": self.result["cycle_id"]})
        for index, selector in enumerate(selectors):
            self._close()
            result = self._begin(**selector, slug=f"reopen-selector-{index}")
            self.assertEqual(result["campaign_id"], self.campaign)
            self.assertTrue(result["campaign_reopened"])
            self.assertTrue(result["campaign_reopen_event_id"])
            P.finalize(self.root, cycle_id=result["cycle_id"], state="abandoned",
                       abandon_reason="operator-decision", allow_open_route=True)

    def test_a24_4_close_reopen_close_has_valid_append_only_sequence(self):
        self._close()
        v1 = (self.path.parent / "campaign.events/000001.json").read_bytes()
        result = self._begin(campaign=self.campaign)
        self.assertTrue(result["campaign_reopened"])
        reopened_bytes = (self.path.parent / "campaign.events/000002.json").read_bytes()
        self.assertEqual(json.loads(reopened_bytes)["payload"]["reopens_event_id"],
                         json.loads(v1)["event_id"])
        P.finalize(self.root, cycle_id=result["cycle_id"], state="abandoned",
                   abandon_reason="operator-decision", allow_open_route=True)
        C.close(self.root, self.path, reason="second completion")
        self.assertEqual(C.campaign_state(self.root, self.path).state, "satisfied")
        self.assertEqual((self.path.parent / "campaign.events/000001.json").read_bytes(), v1)
        self.assertEqual((self.path.parent / "campaign.events/000002.json").read_bytes(), reopened_bytes)
        self.assertEqual([p.name for p in sorted((self.path.parent / "campaign.events").glob("*.json"))],
                         ["000001.json", "000002.json", "000003.json"])

    def test_a24_5_v1_event_is_sequence_one_and_legacy_bytes_are_unchanged(self):
        old = self.path.parent / C.LEGACY_EVENT_NAME
        raw = self._write_v1_close()
        folded = C.campaign_state(self.root, self.path)
        self.assertEqual((folded.state, folded.last_sequence), ("satisfied", 1))
        C.reopen(self.root, self.path, reason="resume legacy campaign")
        self.assertTrue((self.path.parent / "campaign.events/000002.json").exists())
        self.assertEqual(old.read_bytes(), raw)

    def test_a24_6_metadata_write_and_projection_recovery(self):
        with mock.patch.object(C, "_materialize", side_effect=OSError("crash after event")):
            with self.assertRaisesRegex(C.CampaignError, "campaign-close-committed-recovery-required"):
                self._close()
        self.assertTrue(C.campaign_state(self.root, self.path).projection_pending)
        C.recover(self.root, self.path)
        record = json.loads(self.path.read_text()); record["title"] = "updated title"
        P._write_campaign(self.root, record, exclusive=False)
        self.assertEqual(json.loads(self.path.read_text())["title"], "updated title")
        self.assertEqual(C.campaign_state(self.root, self.path).state, "satisfied")

    def test_a24_7_compose_listing_and_locator_use_fold(self):
        self._close()
        rows = P.list_campaign_summaries(self.root, active_only=False)
        row = next(item for item in rows if item["campaign_id"] == self.campaign)
        self.assertEqual(row["state"], "satisfied")
        route = F.compile_for("direct", self.root, slug="compose-closed", gate_source="compose",
                              campaign_key="campaign-closure")
        selection = F.R.compose_campaign_selection(route)
        self.assertEqual(selection["mode"], "reopen")
        self.assertIn("닫힌 캠페인 재개", F.R._compose_campaign_line(selection))
        _, view = C.locator.scan_index(self.root)
        self.assertEqual(view[self.campaign]["status"], "satisfied")

    def test_a24_8_abandoned_campaign_is_not_reopened(self):
        record = json.loads(self.path.read_text())
        for state in ("abandoned", "superseded"):
            record["state"] = state
            P._write_campaign(self.root, record, exclusive=False)
            with self.assertRaisesRegex(P.ProducerError, "campaign-not-active"):
                self._begin(campaign=self.campaign, slug="refuse-" + state)

    def test_a24_9_two_process_closers_publish_one_event(self):
        with mock.patch.object(C.os, "link", side_effect=OSError("precommit")):
            with self.assertRaisesRegex(C.CampaignError, "campaign-close-not-committed"):
                C.close(self.root, self.path, reason="retry after failed publication")
        self.assertEqual(C.campaign_state(self.root, self.path).state, "active")
        args = [sys.executable, P.__file__, "campaign-close", "--artifact-root", str(self.root),
                "--campaign", str(self.path), "--reason", "concurrent completion"]
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            results = list(pool.map(lambda _n: subprocess.run(args, text=True, capture_output=True), range(2)))
        for result in results:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        events = list((self.path.parent / "campaign.events").glob("*.json"))
        self.assertEqual([path.name for path in events], ["000001.json"])
        route = F.compile_for("direct", self.root, slug="parallel-reopen", gate_source="parallel-reopen")
        binding = F.L.admit_runtime_route(self.root, route)
        script = ("import sys,json; from pathlib import Path; sys.path.insert(0,sys.argv[1]); "
                  "import artifact_producer as P; r=P.begin(Path(sys.argv[2]),route_file=Path(sys.argv[3]), "
                  "capability='autopilot-code',intensity='direct',campaign_key='campaign-closure'); "
                  "print(json.dumps(r))")
        begin_args = [sys.executable, "-c", script, str(Path(P.__file__).parent), str(self.root), binding.route_file]
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            begins = list(pool.map(lambda _n: subprocess.run(begin_args, text=True, capture_output=True), range(2)))
        for result in begins:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        reopened = [json.loads(result.stdout) for result in begins]
        self.assertEqual(sum(row.get("campaign_reopened") is True for row in reopened), 1)
        self.assertEqual([path.name for path in sorted((self.path.parent / "campaign.events").glob("*.json"))],
                         ["000001.json", "000002.json"])
        for row in P.list_cycle_records(self.root):
            if row.get("campaign_id") == self.campaign and row.get("state") == "open":
                P.finalize(self.root, cycle_id=row["cycle_id"], state="abandoned",
                           abandon_reason="operator-decision", allow_open_route=True)
        race_route = F.compile_for("direct", self.root, slug="close-begin-race", gate_source="close-begin-race")
        race_binding = F.L.admit_runtime_route(self.root, race_route)
        close_args = [sys.executable, P.__file__, "campaign-close", "--artifact-root", str(self.root),
                      "--campaign", str(self.path), "--reason", "close begin race"]
        begin_args = [sys.executable, "-c", script, str(Path(P.__file__).parent), str(self.root),
                      race_binding.route_file]
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            close_future = pool.submit(subprocess.run, close_args, text=True, capture_output=True)
            begin_future = pool.submit(subprocess.run, begin_args, text=True, capture_output=True)
            close_result, begin_result = close_future.result(), begin_future.result()
        self.assertIn(close_result.returncode, (0, 65), close_result.stdout + close_result.stderr)
        self.assertEqual(begin_result.returncode, 0, begin_result.stdout + begin_result.stderr)
        self.assertEqual(C.campaign_state(self.root, self.path).state, "active")

    def test_a24_11_no_approval_surface_remains(self):
        tokens = ("verify_" + "approval", "--approval-" + "session",
                  "awaiting-user-" + "acceptance", "campaign-" + "satisfy")
        root = Path(__file__).resolve().parent.parent
        hits = []
        for folder in (root / "utilities", root / "docs", root / "core"):
            for path in folder.rglob("*"):
                if path.is_file():
                    try: text = path.read_text(encoding="utf-8")
                    except (UnicodeError, OSError): continue
                    if any(token in text for token in tokens): hits.append(str(path))
        self.assertEqual(hits, [])

    def test_a24_12_old_reader_compatibility_boundaries(self):
        if subprocess.run(["git", "cat-file", "-e", "307a4b3a^{commit}"],
                          cwd=Path(__file__).resolve().parents[1], capture_output=True).returncode:
            self.skipTest("base revision is unavailable in this shallow checkout")
        archive = Path(self._tmp.name) / "old-reader.tar"
        old_root = Path(self._tmp.name) / "old-reader"
        old_root.mkdir()
        with archive.open("wb") as stream:
            subprocess.run(["git", "archive", "307a4b3a", "utilities"],
                           cwd=Path(__file__).resolve().parents[1], check=True, stdout=stream)
        subprocess.run(["tar", "-xf", str(archive), "-C", str(old_root)], check=True)
        old_utilities = old_root / "utilities"
        script = ("import sys,json; from pathlib import Path; sys.path.insert(0,sys.argv[1]); "
                  "import artifact_producer as P; root=Path(sys.argv[2]); action=sys.argv[3]; "
                  "campaign=sys.argv[4]; "
                  "exec(\"try:\\n r=P.read_campaign(root,campaign) if action=='read' else "
                  "P.begin(root,route_file=Path(sys.argv[5]),capability='autopilot-code',intensity='direct',campaign_id=campaign)"
                  "\
 print('state='+str((r or {}).get('state')))\
except Exception as e: print('code='+str(getattr(e,'code',type(e).__name__)))\")")
        self._write_v1_close()
        C.reopen(self.root, self.path, reason="legacy reopen")
        self._begin(campaign=self.campaign, slug="legacy-reader-reopen")
        env = {**os.environ, "HOME": str(Path(self._tmp.name) / "home"),
               "XDG_STATE_HOME": str(Path(self._tmp.name) / "state"),
               "AGENT_ARTIFACT_CHECKPOINT": "off"}
        result = subprocess.run([sys.executable, "-c", script, str(old_utilities), str(self.root),
                                 "read", self.campaign, "-"], text=True, capture_output=True, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("campaign-projection-conflict", result.stdout)

        other_route, other_file, other = self.begin(campaign_key="legacy-reader-v2")
        self.write_output(other); self.close(other_route, other_file)
        P.finalize(self.root, cycle_id=other["cycle_id"])
        other_path = Path(other["cycle_dir"]).parent / "campaign.json"
        C.close(self.root, other_path, reason="v2 closed campaign")
        next_route = F.compile_for("direct", self.root, slug="legacy-reader-old-begin", gate_source="old-begin")
        next_binding = F.L.admit_runtime_route(self.root, next_route)
        result = subprocess.run([sys.executable, "-c", script, str(old_utilities), str(self.root),
                                 "begin", other["campaign_id"], next_binding.route_file],
                               text=True, capture_output=True, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("campaign-not-active", result.stdout)



class OpenRefusalDetailTest(unittest.TestCase):
    def test_open_refusal_detail_is_bounded(self):
        pending = [(f"cyc_{i:032x}", f"rt-{i:016x}") for i in range(12)]
        detail = C._open_refusal_detail(pending)
        self.assertTrue(detail.startswith("open=12 "))
        self.assertIn("+2 more", detail)
        self.assertEqual(detail.count("route_state=open"), 10)


if __name__ == "__main__":
    unittest.main()
