"""Selection pump and bounded source-cache contracts retained by the explorer reader."""

import json
import os
import tempfile
import threading
import time
import unittest

from tools.fleet.explorer.focus import SelectionPump, SourceCache
from tools.fleet.explorer.types import FocusKey, FocusResult, Health


def wait_for(predicate, timeout=3.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.01)
    return predicate()


class GatedProvider:
    def __init__(self):
        self.requests = []
        self.entered = threading.Semaphore(0)
        self.gate = threading.Semaphore(0)

    def __call__(self, request):
        self.requests.append(request)
        self.entered.release()
        self.gate.acquire(timeout=3)
        return FocusResult(request.key, request.epoch, request.snapshot_generation,
                           output=(request.key.sid,), health=Health("success", time.time()))


class SelectionPumpContracts(unittest.TestCase):
    def test_late_result_for_previous_selection_is_dropped(self):
        provider = GatedProvider()
        pump = SelectionPump(provider, interval=0.1)
        a, b = FocusKey("claude", "A"), FocusKey("claude", "B")
        try:
            pump.select(a, 1)
            pump.request()
            self.assertTrue(provider.entered.acquire(timeout=2))
            pump.select(b, 2)
            pump.select(a, 3)
            pump.request()
            provider.gate.release()
            self.assertTrue(provider.entered.acquire(timeout=2))
            self.assertIsNone(pump.poll())
            self.assertEqual([item.epoch for item in provider.requests], [1, 3])
            provider.gate.release()
            result = wait_for(pump.poll)
            self.assertEqual((result.key, result.epoch), (a, 3))
        finally:
            provider.gate.release()
            pump.stop()

    def test_selections_while_reading_coalesce_to_latest(self):
        provider = GatedProvider()
        pump = SelectionPump(provider, interval=0.1)
        try:
            pump.select(FocusKey("claude", "A"), 1)
            pump.request()
            self.assertTrue(provider.entered.acquire(timeout=2))
            for sid in ("B", "C", "D"):
                pump.select(FocusKey("claude", sid), 1)
            pump.request()
            provider.gate.release()
            self.assertTrue(provider.entered.acquire(timeout=2))
            self.assertEqual([item.key.sid for item in provider.requests], ["A", "D"])
            provider.gate.release()
            self.assertEqual(wait_for(pump.poll).key.sid, "D")
        finally:
            provider.gate.release()
            pump.stop()


class SourceCacheContracts(unittest.TestCase):
    def test_unchanged_file_is_read_once_and_cache_is_bounded(self):
        cache = SourceCache(limit=2)
        with tempfile.TemporaryDirectory() as root:
            paths = [os.path.join(root, f"{index}.json") for index in range(3)]
            for index, path in enumerate(paths):
                with open(path, "w", encoding="utf-8") as stream:
                    json.dump({"value": index}, stream)
            self.assertEqual(cache.read_json(paths[0]), {"value": 0})
            reads = cache.reads
            self.assertEqual(cache.read_json(paths[0]), {"value": 0})
            self.assertEqual(cache.reads, reads)
            cache.read_json(paths[1])
            cache.read_json(paths[2])
            self.assertLessEqual(len(cache._items), 2)
            self.assertIsNone(cache.read_json(os.path.join(root, "missing.json")))

    def test_changed_file_is_read_again(self):
        cache = SourceCache()
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "record.json")
            with open(path, "w", encoding="utf-8") as stream:
                json.dump({"value": 1}, stream)
            self.assertEqual(cache.read_json(path), {"value": 1})
            with open(path, "w", encoding="utf-8") as stream:
                json.dump({"value": 2}, stream)
            self.assertEqual(cache.read_json(path), {"value": 2})
            self.assertEqual(cache.reads, 2)


class ParsedRegistryRows(unittest.TestCase):
    def test_focus_uses_b0_parsed_rows_and_keeps_physical_line_numbers(self):
        from unittest import mock
        from tools.fleet.explorer import focus
        class Dispatch:
            @staticmethod
            def parsed_registry_rows(_path):
                return ((4, ("ts", "open", "", "", "attempt", "parent_sid=selected"),
                         {"parent_sid": "selected", "attempt_id": "attempt"}),)
        with mock.patch.object(focus, "_dispatch", return_value=Dispatch):
            rows = focus.session_entries("registry", "selected")
        self.assertEqual(rows, [(4, ["ts", "open", "", "", "attempt", "parent_sid=selected"],
                                 {"parent_sid": "selected", "attempt_id": "attempt"})])




class FakeTidy:
    """The bits of session_tidy the seat reader touches, over a seat written by hand."""

    class Seat:
        def __init__(self, kind, key, pane="", harness="", project_key="", endpoint=""):
            self.kind, self.key, self.pane, self.harness = kind, key, pane, harness
            self.project_key, self.endpoint, self.native, self.sid = project_key, endpoint, None, ""

    def __init__(self, tmp, lineage):
        self.tmp, self.lineage, self.panes = tmp, lineage, []

    def _endpoint_scope(self, env=None):
        return ""

    def _digest(self, *parts, size=20):
        return "|".join(parts)

    def resolve_seat(self, harness, cwd=None, env=None, sid=None, **_):
        return self.Seat("project", "project-seat", harness=harness)

    def seat_lineage(self, seat, sid=None, **_):
        self.panes.append(seat.pane)
        return self.lineage

    def read_json(self, path):
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)


class SeatHistory(unittest.TestCase):
    def setUp(self):
        from tools.fleet.explorer import focus
        self.focus = focus
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        card = os.path.join(self.tmp.name, "card.json")
        with open(card, "w", encoding="utf-8") as handle:
            json.dump({"body": "# first line\nsecond line", "authored_at_epoch": 1_700_000_000.0}, handle)
        self.lineage = [
            {"harness": "claude", "sid": "now-1", "started_at": 1_700_001_000.0, "source": "clear",
             "relation": "confirmed", "cards": [], "transcript": {"path": None, "status": "missing"}},
            {"harness": "claude", "sid": "old-1", "started_at": 1_699_990_000.0, "source": "startup",
             "relation": "chronological",
             "cards": [{"generation": 1, "path": card, "status": "available"}],
             "transcript": {"path": None, "status": "missing"}}]
        self.tidy = FakeTidy(self.tmp.name, self.lineage)
        original = focus.utility
        focus.utility = lambda name: self.tidy if name == "session_tidy" else original(name)
        self.addCleanup(setattr, focus, "utility", original)

    def test_seat_is_read_for_the_sessions_own_pane_oldest_first_current_last(self):
        entries, pane, health = self.focus.read_seat_history({"cwd": "/x"}, "claude", "now-1", pane="w1:p2")
        self.assertEqual(health.state, "success")
        self.assertEqual((self.tidy.panes, pane), (["w1:p2"], "w1:p2"))
        self.assertEqual([(e["sid"], e["current"]) for e in entries], [("old-1", False), ("now-1", True)])
        self.assertEqual(entries[0]["cards"][0]["title"], "first line")     # the newest card is opened

    def test_cards_flag_reads_every_body_and_a_missing_seat_is_none_not_a_guess(self):
        entries, _pane, _health = self.focus.read_seat_history({"cwd": "/x"}, "claude", "now-1", cards=True)
        self.assertEqual(entries[0]["cards"][0]["lines"], ["# first line", "second line"])
        self.tidy.seat_lineage = lambda *_a, **_k: (_ for _ in ()).throw(OSError("gone"))
        entries, _pane, health = self.focus.read_seat_history({"cwd": "/x"}, "claude", "now-1")
        self.assertIsNone(entries)
        self.assertEqual(health.state, "failed")

    def test_generation_chain_follows_compact_clear_and_unknown_starts(self):
        from tools.fleet.explorer import nav
        entries = (
            {"harness": "claude", "sid": "a", "source": "startup", "current": False,
             "cards": [{"status": "available"}, {"status": "available"}]},
            {"harness": "codex", "sid": "b", "source": "clear", "current": False, "cards": [{"status": "expired"}]},
            {"harness": "codex", "sid": "c", "source": "startup", "current": True, "cards": []})
        chain = nav.generation_chain(entries, "codex", "c")
        self.assertEqual([(i["sid"], i["kind"]) for i in chain],
                         [("a", "new"), ("a", "cont"), ("b", "new"), ("c", "new")])
        self.assertIn("clear", chain[2]["connection"])
        self.assertIn("claude → codex", chain[2]["connection"])
        self.assertEqual(chain[3]["connection"], "new session · no handover record")
        self.assertTrue(chain[3]["now"])
        self.assertEqual(nav.default_generation(chain), 1)         # the newest generation that has a card

    def test_old_lineage_shape_is_derived_without_a_second_read(self):
        entries, _pane, _health = self.focus.read_seat_history({"cwd": "/x"}, "claude", "now-1")
        old = self.focus.lineage_from_seat(entries)
        self.assertEqual([item["sid"] for item in old], ["old-1"])
        self.assertEqual(old[0]["title"], "first line")


class AuxReads(unittest.TestCase):
    def test_aux_kinds_read_panes_seats_and_models_and_nothing_else(self):
        from tools.fleet.explorer import focus
        from tools.fleet.explorer.types import FocusRequest
        originals = (focus.read_pane_map, focus.read_seat_history, focus.read_model_config)
        focus.read_pane_map = lambda: {"sid-1": "w1:p1"}
        seen = []

        def seat(session, harness, sid, *, cards=False, pane=None):
            seen.append((harness, sid, cards, pane))
            return ({"sid": sid, "current": True, "cards": []},), pane or "", Health("success", 1.0)
        focus.read_seat_history = seat
        focus.read_model_config = lambda: {"profiles": ["balanced"]}
        snapshot = {"sessions": [{"harness": "claude", "session_id": "sid-1", "cwd": "/x"}]}
        try:
            panes = focus.collect_aux(FocusRequest(FocusKey("*", "panes"), 1, 0))
            seats = focus.collect_aux(FocusRequest(FocusKey("*", "seats"), 1, 0, ("claude:sid-1", "claude:nope")),
                                      snapshot=snapshot)
            models = focus.collect_aux(FocusRequest(FocusKey("*", "models"), 1, 0))
        finally:
            focus.read_pane_map, focus.read_seat_history, focus.read_model_config = originals
        self.assertEqual(panes.source_records["panes"], {"sid-1": "w1:p1"})
        self.assertEqual(seen, [("claude", "sid-1", False, "w1:p1")])        # an unknown session is skipped
        self.assertEqual(list(seats.source_records["seats"]), ["claude:sid-1"])
        self.assertEqual(models.source_records["models"], {"profiles": ["balanced"]})


class ExplorerFeedRequests(unittest.TestCase):
    """What the open screen asks the pumps for: only what is open."""

    @classmethod
    def setUpClass(cls):
        from tools.fleet import render
        from tools.fleet.tests import explorer_fixture as ef
        cls.render = render
        snapshot, hosts = ef.demo_snapshot()
        snapshot.compute_hosts = hosts
        cls.snapshot = snapshot
        render.set_explorer_mode(True)
        render._explorer_compose(snapshot, 140, 45, None)       # the route pages name their session from this
        cls.addClassCleanup(render.set_explorer_mode, False)

    def requests(self, **state):
        from tools.fleet.explorer.state import NavState
        return self.render._explorer_requests(self.snapshot, NavState(**state), self.render._EXPLORER_MODEL)

    def test_the_overview_reads_only_the_panes(self):
        self.assertEqual(set(self.requests()), {"panes"})

    def test_history_reads_every_seat_and_the_opened_seat_in_full(self):
        found = self.requests(view="HISTORY", folds=(("hopen:HISTORY", "a7"),))
        self.assertEqual(set(found), {"panes", "seats", "session"})
        self.assertIn("claude:demo-claude-1", found["seats"][1])
        key, expanded, wants = found["session"]
        self.assertEqual((key.harness, key.sid), ("claude", "demo-claude-1"))
        self.assertEqual(wants, ("seat", "cards", "dialogue:last"))

    def test_a_session_page_reads_its_seat_pane_and_picked_generation(self):
        found = self.requests(scope="session:a7", folds=(("ui:gen:a7", "2"),))
        self.assertEqual(found["session"][2], ("seat", "cards", "pane", "dialogue:2"))

    def test_a_route_page_reads_its_owner_output_and_models_only_on_models(self):
        route = next(r for r in self.render._EXPLORER_MODEL.routes if r.get("session_tag"))
        found = self.requests(scope="route:" + route["id"])
        self.assertTrue(found["session"][2][0].startswith("raw:" + route["id"] + ":"))
        self.assertEqual(set(self.requests(view="MODELS")), {"panes", "models"})

    def test_feed_selects_once_per_change_and_keeps_the_last_good_result(self):
        from tools.fleet.explorer.state import NavState
        from tools.fleet.explorer.types import FocusResult
        calls = []

        def reader(request):
            calls.append((request.key.harness, request.key.sid, request.wants))
            records = {"panes": {"panes": {"sid-1": "w1:p1"}}, "seats": {"seats": {"claude:x": ()}}}
            return FocusResult(request.key, request.epoch, request.snapshot_generation,
                               health=Health("success", time.time()),
                               source_records=records.get(request.key.sid, {}))
        feed = self.render._ExplorerFeed(reader, 0.2)
        try:
            detail, changed = None, False
            deadline = time.time() + 3
            while time.time() < deadline and not (detail and detail.panes):
                detail, changed = feed.update(NavState(), 1, time.monotonic(), self.snapshot)
                time.sleep(0.02)
            self.assertEqual(detail.panes, {"sid-1": "w1:p1"})
            count = len(calls)
            _again, changed = feed.update(NavState(), 1, time.monotonic(), self.snapshot)
            self.assertFalse(changed)
            self.assertEqual(len(calls), count)                 # an unchanged screen does not re-select
            wait_for(lambda: feed.update(NavState(view="HISTORY"), 1, time.monotonic(), self.snapshot)[0].seats)
            detail, _changed = feed.update(NavState(), 1, time.monotonic(), self.snapshot)
            self.assertEqual(detail.panes, {"sid-1": "w1:p1"})  # the pane map stays after leaving HISTORY
            self.assertEqual(list(detail.seats), ["claude:x"])
        finally:
            feed.stop()

    def test_a_read_that_finds_the_same_thing_again_is_not_a_change(self):
        from tools.fleet.explorer.state import NavState
        from tools.fleet.explorer.types import FocusResult
        calls = []

        def reader(request):
            calls.append(request.epoch)
            return FocusResult(request.key, request.epoch, request.snapshot_generation,
                               health=Health("success", time.time()),
                               source_records={"panes": {"sid-1": "w1:p1"}})
        from unittest import mock
        patched = mock.patch.dict(self.render._EXPLORER_INTERVALS, {"panes": 0.05})
        patched.start()
        self.addCleanup(patched.stop)
        feed = self.render._ExplorerFeed(reader, 0.05)
        try:
            wait_for(lambda: feed.update(NavState(), 1, time.monotonic(), self.snapshot)[0] is not None
                     and feed.last is not None and feed.last.panes)
            before = len(calls)
            changes = []
            deadline = time.time() + 1.0
            while time.time() < deadline and len(calls) < before + 2:
                changes.append(feed.update(NavState(), 1, time.monotonic(), self.snapshot)[1])
                time.sleep(0.02)
            self.assertGreaterEqual(len(calls), before + 2)      # it did read again ...
            self.assertFalse(any(changes))                       # ... and found nothing new
        finally:
            feed.stop()


if __name__ == "__main__":
    unittest.main()
