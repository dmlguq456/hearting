"""HISTORY lists seats whose session ended, after the live ones (e39c9d).

The 2026-10-28 repair showed only live seats and left ended ones as the follow-up. An ended
seat is read from the same seat ledgers and lineage graph; it shows its short SID and
``ended`` (a released ``[NN]`` may already belong to a live session), recedes like a finished
route, and a seat whose chain continues in a live session that does not list it is drawn
under that seat in the PEERS handover words. A session a registered attempt announced is a
worker, not a seat; when the registry cannot be read, nothing is hidden.
"""
import os
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from tools.fleet.collectors import dispatch                          # noqa: E402
from tools.fleet.explorer import focus, live                         # noqa: E402
from tools.fleet.explorer.frame import draw                          # noqa: E402
from tools.fleet.explorer.state import NavState                      # noqa: E402
from tools.fleet.explorer.style import recede_key                    # noqa: E402
from tools.fleet.explorer.types import ExplorerDetail                # noqa: E402
from tools.fleet.explorer.view_model import ExplorerViewModel        # noqa: E402

NOW = 2_000_000_000.0
DAY = 86400.0


def _start(sid, ts, cwd="/repo"):
    return {"event": "start", "harness": "claude", "sid": sid, "ts": ts, "first_seen": ts,
            "cwd": cwd, "source": "startup"}


def _entries(sid, title):
    return ({"harness": "claude", "sid": sid, "started_at": NOW - 3600, "source": "startup",
             "relation": None, "transcript": {}, "current": True,
             "cards": [{"title": title, "status": "available", "at": NOW - 600}]},)


class EndedSeatReaderTest(unittest.TestCase):
    def setUp(self):
        focus._ATTEMPT_SESSIONS.clear()
        self.addCleanup(focus._ATTEMPT_SESSIONS.clear)
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.log = os.path.join(temp.name, "worker.claude.jsonl")
        with open(self.log, "w", encoding="utf-8") as stream:
            stream.write('{"type":"system","subtype":"init","session_id":"ended-worker"}\n')
        self.graph = SimpleNamespace(
            ledger_rows={
                "pane-live": [_start("live-old", NOW - 2 * DAY), _start("live-new", NOW - DAY)],
                "pane-ended": [_start("ended-main", NOW - 2 * 3600)],
                "pane-worker": [_start("ended-worker", NOW - 3600, cwd="/repo/wt")],
                "pane-handed": [_start("handed", NOW - 3 * 3600)],
                "pane-other": [_start("live-other", NOW - 3000)],
                "pane-stale": [_start("too-old", NOW - 9 * DAY)],
                "pane-pred": [_start("pred-of-ended", NOW - 5 * 3600)],
            },
            edges=(("claude", "live-old", "live-new"), ("claude", "handed", "live-other"),
                   ("claude", "pred-of-ended", "ended-main")),
            unresolved=frozenset())
        self.shown = {("claude", "live-new"), ("claude", "live-old"), ("claude", "live-other")}
        self.registry = os.path.join(temp.name, "jobs.log")
        with open(self.registry, "w", encoding="utf-8") as stream:
            stream.write("2033-05-18T00:00:00Z\topen\t/repo/wt\t/repo\tworker\t"
                         "harness=claude,log_file=%s\n" % self.log)
        self.addCleanup(focus._REGISTRY_TAILS.pop, self.registry, None)

    def read(self, broken=False):
        handover = focus.utility("dispatch_seat_handover")
        tail = (mock.patch.object(focus, "_registry_attempt_logs", side_effect=PermissionError("no"))
                if broken else mock.patch.object(focus, "_REGISTRY_TAILS", {}))
        with mock.patch.object(handover, "_pass_lineage_graph", return_value=self.graph), \
             mock.patch.object(focus, "_seat_entries",
                               side_effect=lambda _m, _seat, _h, sid, **_k: _entries(sid, sid)), \
             mock.patch.object(dispatch, "_candidate_jobs_paths", return_value=[self.registry]), \
             tail:
            return focus.read_ended_seats(self.shown, now=NOW)

    def test_ended_main_seats_only_newest_first(self):
        found = [(item["sid"], item["successor"]) for item in self.read()]
        self.assertEqual(found, [("ended-main", None), ("handed", ("claude", "live-other"))])

    def test_an_unreadable_registry_hides_nothing(self):
        found = [item["sid"] for item in self.read(broken=True)]
        self.assertIn("ended-worker", found)
        self.assertIn("ended-main", found)


class RegistryTailTest(unittest.TestCase):
    def test_the_registry_is_read_only_where_it_grew(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        path = os.path.join(temp.name, "jobs.log")
        row = "2033-05-18T00:00:%02dZ\topen\t/repo/wt\t/repo\tslug\tharness=claude,log_file=/logs/%s.jsonl\n"
        with open(path, "w") as stream:
            stream.write(row % (1, "one"))
        focus._REGISTRY_TAILS.pop(path, None)
        self.addCleanup(focus._REGISTRY_TAILS.pop, path, None)
        self.assertEqual(focus._registry_attempt_logs(path), {"/repo/wt": [
            ("2033-05-18T00:00:01Z", "/logs/one.jsonl")]})
        first = focus._REGISTRY_TAILS[path]["offset"]
        with open(path, "a") as stream:
            stream.write(row % (2, "two") + "2033-05-18T00:00:03Z\topen\t/repo/wt")   # a half row
        reads = []
        real_open = open

        def counted(file, mode="r", *args, **kwargs):
            if file == path:
                reads.append(mode)
            return real_open(file, mode, *args, **kwargs)

        with mock.patch("builtins.open", side_effect=counted):
            logs = focus._registry_attempt_logs(path)
            focus._registry_attempt_logs(path)              # nothing new: no read at all
        self.assertEqual([log for _ts, log in logs["/repo/wt"]], ["/logs/one.jsonl", "/logs/two.jsonl"])
        self.assertEqual(len(reads), 2, "the half row is read again once it is complete")
        self.assertGreater(focus._REGISTRY_TAILS[path]["offset"], first)


class EndedSeatHistoryTest(unittest.TestCase):
    def view(self):
        rows = [{"tag": "34", "harness": "claude", "repo": "hearting", "repo_id": "hearting",
                 "state": "idle", "title": "live seat"},
                {"tag": "50", "harness": "claude", "repo": "hearting", "repo_id": "hearting",
                 "state": "idle", "title": "other live seat"}]
        index = {"34": {"sid": "live-new0000", "ids": ("live-new0000",)},
                 "50": {"sid": "live-other00", "ids": ("live-other00",)}}
        ended = (
            {"harness": "claude", "sid": "ab12cd34-ended", "cwd": "/home/x/hearting",
             "ended_at": NOW - 2 * 3600, "successor": None,
             "entries": _entries("ab12cd34-ended", "an ended seat's last card")},
            {"harness": "claude", "sid": "ef56ab78-handed", "cwd": "/home/x/hearting",
             "ended_at": NOW - 3 * 3600, "successor": ("claude", "live-other00"),
             "entries": _entries("ef56ab78-handed", "handed over")})
        seats = {"claude:live-new0000": _entries("live-new0000", "live card"),
                 "claude:live-other00": _entries("live-other00", "other live card")
                 + _entries("live-other00", "second generation"),
                 live.ENDED_SEATS: ended}
        detail = ExplorerDetail(focus=None, seats=seats, config=None, panes={}, results={})
        seat_rows, generations, cards, *_rest = live._detail_sections(
            rows, index, detail, NOW, lambda _cwd: "hearting")
        vm = ExplorerViewModel.from_mapping({
            "repos": [{"id": "hearting", "name": "hearting"}], "sessions": rows,
            "seats": seat_rows, "generations": generations, "cards": cards})
        return seat_rows, draw(vm, NavState(view="HISTORY", title="HISTORY"), 168, 45, beat=1)

    def test_history_shows_ended_seats_after_live_ones(self):
        seat_rows, grid = self.view()
        self.assertEqual([seat[0] for seat in seat_rows],
                         ["34", "50", "~claude:ab12cd34-ended", "~claude:ef56ab78-handed"])
        self.assertEqual(seat_rows[2][2], 1, "an ended seat has no running `now` generation")
        self.assertEqual(seat_rows[0][2], 2, "a live seat keeps its `now` generation")
        lines = ["".join(str(cell[0] or "") for cell in row) for row in grid.cells]
        live_row = next(i for i, line in enumerate(lines) if "[50]" in line)
        handed = next(i for i, line in enumerate(lines) if "ef56ab78" in line)
        ended = next(i for i, line in enumerate(lines) if "ab12cd34" in line)
        self.assertEqual(handed, live_row + 1, "drawn under the live seat it continued in")
        generations = dict((seat[0], seat[2]) for seat in seat_rows)["50"]
        self.assertIn(f"gen {generations - 1} → [50]", lines[handed])  # the PEERS handover words
        self.assertGreater(ended, handed)
        self.assertIn("ended", lines[ended])
        self.assertIn("an ended seat's last card", lines[ended])
        self.assertNotIn("[", lines[ended].split("ab12cd34")[0][-6:], "no released [NN] tag")
        keys = {grid.cells[ended][column][1] for column in range(40, 160)
                if str(grid.cells[ended][column][0] or "").strip()}
        live_keys = {grid.cells[live_row][column][1] for column in range(40, 160)
                     if str(grid.cells[live_row][column][0] or "").strip()}
        self.assertIn("hb_claude", live_keys)
        self.assertNotIn("hb_claude", keys, "an ended seat recedes like a finished route")
        self.assertIn(recede_key("hb_claude"), keys)

    def test_ended_seat_ids_never_collide(self):
        """OpenCode ids share ``ses_``, Codex ids a time-ordered prefix: keys are whole ids, and the
        shown id grows only where two would read the same."""
        ended = tuple({"harness": "opencode", "sid": sid, "cwd": "/home/x/hearting",
                       "ended_at": NOW - 3600, "successor": None, "entries": _entries(sid, sid)}
                      for sid in ("ses_eacd1111aaaa", "ses_eacd2222bbbb", "ses_ffff0000cccc"))
        rows, _generations, _cards = live._ended_seat_rows(ended, [], {}, NOW, lambda _cwd: "x")
        self.assertEqual(len({seat[0] for seat in rows}), 3)
        self.assertEqual([live.ended_seat(seat)["sid"] for seat in rows],
                         ["ses_eacd1", "ses_eacd2", "ses_ffff"])

    def test_without_ended_seats_history_keeps_its_columns(self):
        rows = [{"tag": "34", "harness": "claude", "repo": "hearting", "repo_id": "hearting",
                 "state": "idle", "title": "live seat"}]
        index = {"34": {"sid": "live-new0000", "ids": ("live-new0000",)}}
        detail = ExplorerDetail(focus=None, seats={"claude:live-new0000": _entries(
            "live-new0000", "live card")}, config=None, panes={}, results={})
        seat_rows, generations, cards, *_rest = live._detail_sections(rows, index, detail, NOW)
        self.assertEqual([seat[0] for seat in seat_rows], ["34"])
        self.assertIsNone(live.ended_seat(seat_rows[0]))


if __name__ == "__main__":
    unittest.main()
