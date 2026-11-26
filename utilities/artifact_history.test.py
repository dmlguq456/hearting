#!/usr/bin/env python3
"""Tests for `artifact_history.py`: the one recorder's line format, append-only publishing, and replay.

Everything runs in temporary roots; nothing touches a real artifact root.
"""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import hashlib
import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import artifact_admission as adm  # noqa: E402
import artifact_history as H  # noqa: E402
from split_module_fixture import patch_split  # noqa: E402

TXN = "htxn_" + "5" * 32
EXAMPLE = {"schema_version": 1, "contract": "artifact-history/v1", "event_id": "hevt_" + "4" * 32,
           "transaction_id": TXN, "at": "2026-10-01T09:00:00Z",
           "actor": {"by": "agent", "session": "att-3521eef711234e77a156c5d972e7f628", "harness": None,
                     "route": None, "attempt": None}, "kind": "meta",
           "target": {"type": "campaign", "id": "camp_" + "2" * 32, "path": "campaigns/2026-10-01_example/meta.json"},
           "operation": "update", "field": "campaign.title", "before": {"value": "명령어 모델 실험"},
           "after": {"value": "명령어 모델 집 적응 비교"}, "reason": "사용자 요청에 따른 제목 수정"}
CLOSE = {"state": "completed", "manifest_digest": "sha256:" + "a" * 64, "revision_id": "rev_20261001T103000Z",
         "files": 12, "excluded": 0}
LIFECYCLE_EXAMPLE = {
    "schema_version": 1, "contract": "artifact-history/v1", "event_id": "hevt_" + "6" * 32,
    "transaction_id": "htxn_" + "7" * 32, "at": "2026-10-01T10:30:00Z",
    "actor": {"by": "agent", "session": "att-3521eef711234e77a156c5d972e7f628", "harness": "claude",
              "route": "rt-379f8a5e9ad09c54", "attempt": "att-3521eef711234e77a156c5d972e7f628"},
    "kind": "lifecycle", "target": {"type": "cycle", "id": "cyc_" + "3" * 32,
                                    "path": "campaigns/2026-10-01_example/2026-10-01_first-cycle"},
    "operation": "update", "field": "state", "before": {"value": "open"}, "after": {"value": CLOSE},
    "reason": "route rt-379f8a5e9ad09c54 closed the cycle"}
NOW = 1_790_000_000.0
MONTH_2 = 1_793_000_000.0  # a later UTC month


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def event(**over):
    base = dict(kind="meta", target_type="campaign", target_id="camp_" + "2" * 32,
                target_path="campaigns/x/meta.json", operation="update", field="campaign.title",
                before=H.value_ref("a"), after=H.value_ref("b"), reason="테스트", actor_by="human",
                transaction_id=TXN, now=NOW)
    base["field"] = over.pop("field_name", base["field"])
    base.update(over)
    return H.make_event(**base)


class HistoryBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "artifact-root"
        self.root.mkdir()

    def locked(self):
        outer = self

        class Lock:
            def __enter__(self):
                self.fd = adm._acquire_lock(outer.root.resolve(), 5)

            def __exit__(self, *exc):
                adm._release_lock(outer.root.resolve(), self.fd)

        return Lock()

    def files(self):
        base = self.root / H.HISTORY_REL
        return sorted(p for p in base.rglob("*.jsonl")) if base.exists() else []


class EventFormatTest(HistoryBase):
    def test_example_line_is_exactly_the_published_example(self):
        validated = H.validate_event(json.loads(json.dumps(EXAMPLE)))
        line = H.event_bytes(validated).decode("utf-8")
        self.assertTrue(line.endswith("\n"))
        self.assertEqual(line.count("\n"), 1)
        self.assertEqual(json.loads(line), EXAMPLE)
        self.assertEqual(list(json.loads(line)), list(H.KEYS))  # key order is part of the format

    def test_small_values_are_kept_and_large_ones_become_digest_and_size(self):
        small = H.value_ref({"k": "가" * 100})
        self.assertEqual(small, {"value": {"k": "가" * 100}})
        big_value = "가" * 400  # 1200 UTF-8 bytes + quotes
        big = H.value_ref(big_value)
        raw = json.dumps(big_value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        self.assertEqual(big, {"digest": "sha256:" + hashlib.sha256(raw).hexdigest(), "bytes": len(raw)})
        edge = "a" * 510  # canonical bytes: 510 + 2 quotes = 512 -> still a value
        self.assertIn("value", H.value_ref(edge))
        self.assertIn("digest", H.value_ref("a" * 511))
        self.assertEqual(H.value_ref(None), {"value": None})

    def test_file_bytes_are_always_a_digest(self):
        self.assertEqual(H.file_ref(b"x"), {"digest": "sha256:" + hashlib.sha256(b"x").hexdigest(), "bytes": 1})
        self.assertEqual(H.file_ref(None), {"value": None})

    def test_every_kind_and_target_type_is_accepted_by_the_one_function(self):
        for kind, target_type in (("meta", "campaign"), ("meta", "cycle"), ("meta", "project"), ("group", "group"),
                                  ("flow", "flow"), ("artifact", "artifact")):
            for operation in ("add", "update", "move", "delete"):
                made = event(kind=kind, target_type=target_type, operation=operation,
                             before=H.file_ref(b"old") if kind == "artifact" else H.value_ref(1),
                             after=H.file_ref(b"new") if kind == "artifact" else H.value_ref(2))
                self.assertEqual((made["kind"], made["target"]["type"], made["operation"]),
                                 (kind, target_type, operation))

    def test_lifecycle_example_line_is_exactly_the_published_example(self):
        line = H.event_bytes(H.validate_event(json.loads(json.dumps(LIFECYCLE_EXAMPLE)))).decode("utf-8")
        self.assertEqual(json.loads(line), LIFECYCLE_EXAMPLE)

    def test_only_by_is_required_and_the_actor_is_closed(self):
        self.assertEqual(event()["actor"], {"by": "human", "session": None, "harness": None, "route": None,
                                            "attempt": None})
        self.assertEqual(event(session="att-abc")["actor"]["session"], "att-abc")
        full = {"by": "rule", "session": "s1", "harness": "claude", "route": "rt-1", "attempt": "att-1"}
        self.assertEqual(event(actor_by=None, actor=full)["actor"], full)
        self.assertEqual(event(actor_by=None, actor={"by": "model"})["actor"]["route"], None)
        for bad in ({"actor_by": "robot"}, {"session": "bad session"}, {"session": ""}, {"route": "a b"},
                    {"harness": "x" * 129}, {"actor_by": None, "actor": {"session": "s"}},
                    {"actor_by": None, "actor": {"by": "human", "extra": 1}},
                    {"actor": {"by": "human"}}):  # actor plus actor_by is ambiguous
            with self.subTest(bad), self.assertRaises(H.HistoryError):
                event(**bad)
        loose = json.loads(json.dumps(EXAMPLE))
        loose["actor"] = {"by": "agent", "session": None}  # the old two-key actor is no longer a line
        with self.assertRaises(H.HistoryError):
            H.validate_event(loose)

    def test_actor_from_env_is_agent_with_its_markers_and_otherwise_human(self):
        self.assertEqual(H.actor_from_env(env={}), {"by": "human", "session": None, "harness": None,
                                                    "route": None, "attempt": None})
        self.assertEqual(H.actor_from_env("rule", env={})["by"], "rule")
        self.assertEqual(H.actor_from_env(env={"AGENT_DISPATCH_JOBS": "/x"})["by"], "human")  # not a session marker
        agent = H.actor_from_env(env={"AGENT_DISPATCH_ATTEMPT_ID": "att-1", "AGENT_ROUTE_ID": "rt-1",
                                      "AGENT_DISPATCH_CURRENT_HARNESS": "codex"})
        self.assertEqual(agent, {"by": "agent", "session": "att-1", "harness": "codex", "route": "rt-1",
                                 "attempt": "att-1"})
        self.assertEqual(H.actor_from_env("rule", env={"AGENT_ROUTE_ID": "rt-2"})["by"], "agent")
        odd = H.actor_from_env(env={"AGENT_ROUTE_ID": "rt 1", "AGENT_DISPATCH_ATTEMPT_ID": "att-1"})
        self.assertEqual((odd["by"], odd["route"]), ("agent", None))  # a bad marker is dropped, never an error
        with mock.patch.dict(os.environ, {"AGENT_ROUTE_ID": "rt-9"}, clear=True):
            self.assertEqual(H.actor_from_env()["route"], "rt-9")
        with self.assertRaises(H.HistoryError):
            H.actor_from_env("robot", env={})
        self.assertEqual(event(actor_by=None, actor=agent)["actor"], agent)

    def test_lifecycle_close_needs_the_closed_manifest_summary(self):
        def life(**over):
            base = dict(kind="lifecycle", target_type="cycle", field_name="state", before=H.value_ref("open"),
                        after=H.value_ref(CLOSE))
            base.update(over)
            return event(**base)

        self.assertEqual(life()["after"], {"value": CLOSE})
        self.assertEqual(life(after=H.value_ref(dict(CLOSE, state="abandoned")))["after"]["value"]["state"], "abandoned")
        for bad in (dict(CLOSE, state="open"), dict(CLOSE, manifest_digest="sha256:short"),
                    dict(CLOSE, files=-1), dict(CLOSE, excluded="0"), dict(CLOSE, files=True),
                    {k: v for k, v in CLOSE.items() if k != "revision_id"}, dict(CLOSE, extra=1)):
            with self.subTest(bad), self.assertRaises(H.HistoryError):
                life(after=H.value_ref(bad))
        with self.assertRaises(H.HistoryError):
            life(operation="add")
        with self.assertRaises(H.HistoryError):
            life(after=H.value_ref("completed"))

    def test_lifecycle_delete_keeps_the_removed_manifest_digest_and_path(self):
        gone = {"manifest_digest": "sha256:" + "b" * 64, "path": "campaigns/c/cycle"}

        def delete(**over):
            base = dict(kind="lifecycle", target_type="cycle", field_name="state", operation="delete",
                        before=H.value_ref(gone), after=H.value_ref(None))
            base.update(over)
            return event(**base)

        self.assertEqual(delete()["before"], {"value": gone})
        self.assertIsNone(delete(before=H.value_ref(dict(gone, manifest_digest=None)))["before"]["value"]["manifest_digest"])
        self.assertEqual(delete(target_type="campaign")["target"]["type"], "campaign")
        for bad in (dict(before=H.value_ref("campaigns/c/cycle")), dict(before=H.value_ref({"path": "a"})),
                    dict(before=H.value_ref(dict(gone, path="/abs"))),
                    dict(before=H.value_ref(dict(gone, manifest_digest="x"))),
                    dict(after=H.value_ref(1)), dict(field_name="parent")):
            with self.subTest(bad), self.assertRaises(H.HistoryError):
                delete(**bad)

    def test_lifecycle_fields_and_targets_are_a_closed_list(self):
        moved = dict(kind="lifecycle", target_type="cycle", operation="move",
                     before=H.value_ref({"campaign_id": "camp_a", "path": "campaigns/a/c"}),
                     after=H.value_ref({"campaign_id": "camp_b", "path": "campaigns/b/c"}))
        for field in ("campaign", "parent", "disposition", "path"):
            self.assertEqual(event(field_name=field, **moved)["field"], field)
        # `cycle-mark --primary` re-points the representative artifact
        self.assertEqual(event(field_name="primary", **dict(moved, operation="update",
                                                           before=H.value_ref("plans/plan.md"),
                                                           after=H.value_ref("reports/report.md")))["field"], "primary")
        self.assertEqual(event(field_name="state", **dict(moved, target_type="campaign", operation="update",
                                                         before=H.value_ref("open"), after=H.value_ref("closed")))
                         ["after"], {"value": "closed"})
        for bad in (dict(field_name="title"), dict(target_type="project"), dict(target_type="artifact"),
                    dict(field_name="state", target_type="campaign", operation="update",
                         before=H.value_ref("open"), after=H.value_ref({"a": 1}))):
            with self.subTest(bad), self.assertRaises(H.HistoryError):
                event(**{**moved, **bad})
        # the file/flow kinds keep their free field paths
        self.assertEqual(event(kind="artifact", target_type="artifact", field_name="plans/plan.md",
                               before=H.file_ref(b"a"), after=H.file_ref(b"b"))["field"], "plans/plan.md")

    def test_invalid_events_are_refused(self):
        for bad in (dict(kind="note"), dict(target_type="folder"), dict(operation="rename"),
                    dict(target_path="/abs/meta.json"), dict(target_path="a/../b"), dict(reason=""),
                    dict(transaction_id="txn_1"), dict(event_id="evt_1"), dict(field="")):
            with self.subTest(bad), self.assertRaises(H.HistoryError):
                event(**bad)
        loose = dict(EXAMPLE, extra=1)
        with self.assertRaises(H.HistoryError):
            H.validate_event(loose)
        with self.assertRaises(H.HistoryError):
            H.validate_event(dict(EXAMPLE, before={"value": 1, "digest": "x"}))

    def test_ids_have_the_documented_prefixes(self):
        self.assertRegex(H.new_event_id(), r"hevt_[0-9a-f]{32}\Z")
        self.assertRegex(H.new_transaction_id(), r"htxn_[0-9a-f]{32}\Z")


class PublishTest(HistoryBase):
    def test_one_file_per_event_in_its_utc_month_with_one_lf_line(self):
        first, second = event(now=NOW), event(now=MONTH_2)
        with self.locked():
            paths = H.publish_events_locked(self.root, [first, second])
        self.assertEqual(len(paths), 2)
        months = sorted(p.parent.name for p in self.files())
        self.assertEqual(months, [first["at"][:7], second["at"][:7]])
        self.assertNotEqual(months[0], months[1])
        for path, expected in zip(self.files(), sorted((first, second), key=lambda e: e["at"])):
            self.assertEqual(path.name, f"{expected['event_id']}.jsonl")
            raw = path.read_bytes()
            self.assertEqual(raw, H.event_bytes(expected))
            self.assertEqual(raw.count(b"\n"), 1)

    def test_published_files_are_never_modified_and_replay_is_a_no_op(self):
        one = event()
        with self.locked():
            H.publish_events_locked(self.root, [one])
        path = self.files()[0]
        before = (sha(path), path.stat().st_mtime_ns, path.stat().st_ino)
        with self.locked():
            H.publish_events_locked(self.root, [one])
            H.publish_events_locked(self.root, [one])
        self.assertEqual((sha(path), path.stat().st_mtime_ns, path.stat().st_ino), before)
        self.assertEqual(len(self.files()), 1)

    def test_same_event_id_with_other_bytes_is_a_conflict_and_nothing_is_overwritten(self):
        one = event()
        other = dict(one, reason="다른 이유")
        with self.locked():
            H.publish_events_locked(self.root, [one])
            digest = sha(self.files()[0])
            with self.assertRaises(H.HistoryError) as ctx:
                H.publish_events_locked(self.root, [other])
        self.assertEqual(ctx.exception.code, "event-conflict")
        self.assertEqual(sha(self.files()[0]), digest)

    def test_publishing_needs_the_admission_lock_and_unique_ids(self):
        with self.assertRaises(H.HistoryError) as ctx:
            H.publish_events_locked(self.root, [event()])
        self.assertEqual(ctx.exception.code, "admission-lock-required")
        one = event()
        with self.locked(), self.assertRaises(H.HistoryError):
            H.publish_events_locked(self.root, [one, one])
        self.assertEqual(self.files(), [])

    def test_a_new_month_never_touches_existing_files(self):
        old = event(now=NOW)
        with self.locked():
            H.publish_events_locked(self.root, [old])
        snapshot = {p: (sha(p), p.stat().st_mtime_ns) for p in self.files()}
        with self.locked():
            H.publish_events_locked(self.root, [event(now=MONTH_2)])
        for path, expected in snapshot.items():
            self.assertEqual((sha(path), path.stat().st_mtime_ns), expected)
        self.assertEqual(len(self.files()), 2)

    def test_no_partial_line_is_ever_visible_and_staging_is_cleaned(self):
        with self.locked():
            H.publish_events_locked(self.root, [event(), event()])
        staging = self.root / H.STAGING_REL
        self.assertEqual([p for p in staging.iterdir()], [])
        with mock.patch.object(H.os, "link", side_effect=OSError("disk full")), self.locked():
            with self.assertRaises(H.HistoryError) as ctx:
                H.publish_events_locked(self.root, [event()])
        self.assertEqual(ctx.exception.code, "history-write-failed")
        self.assertEqual(len(self.files()), 2)  # only the two published before the failure
        self.assertEqual([p for p in staging.iterdir()], [])

    def test_iter_events_reads_back_what_was_published_and_skips_damage(self):
        one = event()
        with self.locked():
            H.publish_events_locked(self.root, [one])
        broken = self.files()[0].parent / ("hevt_" + "9" * 32 + ".jsonl")
        broken.write_text("{not json}\n", encoding="utf-8")
        self.assertEqual(list(H.iter_events(self.root)), [one])
        self.assertTrue(broken.exists())  # damage is skipped, never repaired or deleted

    def test_history_directory_is_the_documented_location(self):
        self.assertEqual(H.HISTORY_REL, ".runtime/artifact-producer/v1/history")
        self.assertIsNotNone(re.fullmatch(r"\d{4}-\d{2}", event()["at"][:7]))


class PublishFailureAndLatestTest(HistoryBase):
    def latest(self):
        return json.loads((self.root / H.LATEST_REL).read_text(encoding="utf-8"))

    def test_every_publish_failure_is_the_typed_exception_with_its_code(self):
        self.assertTrue(issubclass(H.HistoryPublishError, H.HistoryError))
        one = event()
        with self.assertRaises(H.HistoryPublishError) as ctx:
            H.publish_events_locked(self.root, [one])
        self.assertEqual(ctx.exception.code, "admission-lock-required")
        with self.locked():
            H.publish_events_locked(self.root, [one])
            for events, code in (([dict(one, reason="다른 이유")], "event-conflict"), ([one, one], "event-duplicate"),
                                 ([dict(one, kind="note")], "event-invalid")):
                with self.subTest(code), self.assertRaises(H.HistoryPublishError) as ctx:
                    H.publish_events_locked(self.root, events)
                self.assertEqual(ctx.exception.code, code)
        with mock.patch.object(H.admission, "_acquire_lock", side_effect=adm.AdmissionBusy("busy")):
            with self.assertRaises(H.HistoryPublishError) as ctx:
                H.publish_events(self.root, [event()], lock_timeout=0)
        self.assertEqual(ctx.exception.code, "admission-busy")

    def test_an_identical_republish_creates_no_file_and_leaves_latest_alone(self):
        first, second = event(now=NOW), event(now=NOW + 5)
        with self.locked():
            H.publish_events_locked(self.root, [first, second])
            latest = self.root / H.LATEST_REL
            before = (latest.read_bytes(), latest.stat().st_mtime_ns, latest.stat().st_ino, len(self.files()))
            self.assertEqual(len(H.publish_events_locked(self.root, [first])), 1)
            H.publish_events_locked(self.root, [first, second])
        self.assertEqual((latest.read_bytes(), latest.stat().st_mtime_ns, latest.stat().st_ino, len(self.files())), before)
        self.assertEqual(self.latest()["event_id"], second["event_id"])

    def test_latest_names_the_last_published_event_and_the_cumulative_count(self):
        a, b, c = event(now=NOW), event(now=NOW + 5), event(now=MONTH_2)
        self.assertFalse((self.root / H.LATEST_REL).exists())
        with self.locked():
            H.publish_events_locked(self.root, [a, b])
        self.assertEqual(self.latest(), {"event_id": b["event_id"], "at": b["at"], "count": 2})
        with self.locked():
            H.publish_events_locked(self.root, [b, c])  # b repeats; only c is new
        self.assertEqual(self.latest(), {"event_id": c["event_id"], "at": c["at"], "count": 3})
        self.assertEqual(list(self.latest()), ["event_id", "at", "count"])
        self.assertEqual(len(self.files()), 3)  # LATEST.json is not an event
        self.assertEqual(len(list(H.iter_events(self.root))), 3)
        leftovers = [p.name for p in (self.root / H.HISTORY_REL).iterdir() if p.is_file()]
        self.assertEqual(leftovers, ["LATEST.json"])  # no temp file stays

    def test_latest_failure_does_not_fail_the_publish_and_the_next_publish_re_syncs(self):
        a, b = event(now=NOW), event(now=NOW + 5)
        with self.locked(), patch_split(H.producer, "_write_atomic", side_effect=OSError("read-only")):
            paths = H.publish_events_locked(self.root, [a])
        self.assertEqual(len(paths), 1)
        self.assertEqual(len(self.files()), 1)
        self.assertFalse((self.root / H.LATEST_REL).exists())
        with self.locked():
            H.publish_events_locked(self.root, [b])
        self.assertEqual(self.latest(), {"event_id": b["event_id"], "at": b["at"], "count": 2})

    def test_latest_follows_the_lines_made_visible_before_a_failure(self):
        a, b = event(now=NOW), event(now=NOW + 5)
        with self.locked():
            H.publish_events_locked(self.root, [a])
            with self.assertRaises(H.HistoryPublishError):
                H.publish_events_locked(self.root, [b, dict(a, reason="다른 이유")])
        self.assertEqual(self.latest(), {"event_id": b["event_id"], "at": b["at"], "count": 2})

    def test_publish_events_takes_the_admission_lock_and_releases_it(self):
        seen = []
        real = H._publish_locked

        def spy(root, events, created):
            seen.append(adm.holds_lock(root))
            return real(root, events, created)

        one = event()
        self.assertFalse(adm.holds_lock(self.root))
        with mock.patch.object(H, "_publish_locked", spy):
            paths = H.publish_events(self.root, [one])
        self.assertEqual((seen, len(paths)), ([True], 1))
        self.assertFalse(adm.holds_lock(self.root))
        self.assertEqual(self.latest()["event_id"], one["event_id"])
        with self.locked():  # a caller already inside the lock is not locked twice
            self.assertEqual(H.publish_events(self.root, [one]), paths)
        self.assertFalse(adm.holds_lock(self.root))


if __name__ == "__main__":
    unittest.main()
