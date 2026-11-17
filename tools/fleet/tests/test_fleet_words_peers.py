"""Fleet in the shell panel's words and forms, and what MESSAGES, REQUEST and PEERS mean (11-16).

The session page's field names, CTX line, answer-wait mark and word, time forms, section heads,
a failed route's result word, its MESSAGES and REQUEST lines, and the PEERS tab's count and
LINKS, each built from synthetic records through the real live builder and renderer.
"""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../../run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import json
import re
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from tools.fleet.explorer import live, peers
from tools.fleet.explorer.frame import draw
from tools.fleet.explorer.state import NavState, reduce
from tools.fleet.explorer.style import normalize_style

NOW = time.mktime((2026, 11, 16, 22, 53, 0, 0, 0, -1))      # local time, so HH:MM reads the same anywhere
X = 34                                                     # where a page's body starts


def clock(epoch):
    return time.strftime("%H:%M", time.localtime(epoch))


def iso(epoch):
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(epoch))


def raw_session(tag, harness="claude", state="idle", **values):
    row = {"harness": harness, "session_id": "sid-" + tag, "session_tag": tag, "cwd": "/synthetic/hearting",
           "liveness": state, "title": "title " + tag, "summary": "now " + tag, "context_pct": 42,
           "window": 1.0}
    row.update(values)
    return row


def request_record(ident="rq-0a1b2c", state="final", created=NOW - 6000, closed=NOW - 300,
                   requester="f5", recipient="a3"):
    reports = ([{"n": 1, "revision": 1, "kind": {"final": "final", "partial": "partial"}.get(state, "final"),
                 "result": "", "transfer_ref": "b" * 32, "at": closed, "late": False}]
               if state in ("final", "partial") else [])
    return {"schema": "peer-request-v1", "id": ident, "title": "review the popup keys",
            "created": created, "updated": closed if reports else created,
            "requester": {"harness": "claude", "session_id": "sid-" + requester, "name": requester},
            "recipient": {"harness": "codex", "session_id": "sid-" + recipient, "name": recipient},
            "state": state, "revisions": [{"n": 1, "label": "", "transfer_ref": "a" * 32, "created": created,
                                           "state": "current", "followups": []}],
            "reports": reports, "cancel": None, "sending": []}


def peer_observation(requests=(), sent=(), received_rows=None):
    """What ``details._peer_requests`` hands over (sent records, request records and their rows)."""
    records = {record["id"]: record for record in requests}
    sides = {}
    for record in requests:
        sides.setdefault("claude:sid-" + record["requester"]["name"], []).append([record["id"], "out"])
        sides.setdefault("codex:sid-" + record["recipient"]["name"], []).append([record["id"], "in"])
    return {"sent": {"claude:sid-f5": list(sent)}, "requests": records, "sides": sides,
            "rows": received_rows or {"a" * 32: {"state": "received"}, "b" * 32: {"state": "received"}},
            "refs": ["a" * 32, "b" * 32] if requests else []}


def model(sessions, observation=None, extra=None):
    peers_seen = {"state": "success", "observed_at": NOW}
    if observation is not None:
        peers_seen["messages"] = observation
    snapshot = {"sessions": sessions, "resources": [], "observations": {"optional:peers": peers_seen}}
    snapshot.update(extra or {})
    return live.build_view_model(snapshot, now=NOW)


def rows(vm, state, cols=120, lines=45):
    return draw(vm, state, cols, lines, beat=1).text_rows()


def body(text_rows):
    return [row[X:].rstrip() for row in text_rows]


def mesh(received=(), sent=(), requests=(), steward=True, notice=True):
    """f5 with a peer message received from e4, a runtime notice, a request to a3, and a steward 09."""
    f5 = raw_session("f5", active_context_tokens=415000, mtime=NOW - 3600, _prompt_display={
        "human": None, "human_state": "none", "received": [
            {"source": "peer", "sender": "[e4]", "sender_harness": "claude", "sender_session_id": "sid-e4",
             "text": "compare the session window", "at": NOW - 3 * 3600},
            {"source": "runtime", "sender": "runtime", "text": "route finished", "at": NOW - 600},
            *received]})
    if notice:
        f5["peer_last_recv"] = {"from_session_id": None, "from_harness": "runtime", "kind": "notice",
                                "age_min": 44}
    sessions = [f5, raw_session("e4", state="working"), raw_session("a3", "codex")]
    if steward:
        sessions.append(raw_session("09", steward=True, steward_targets=[
            {"harness": "claude", "session_id": "sid-f5", "name": "f5", "kind": "watch",
             "ts": iso(NOW - 8000), "source": "watch"}]))
    return model(sessions, peer_observation(requests, sent))


class FieldNames(unittest.TestCase):
    """1 · The session page's field names are the shell panel's dim upper-case words."""

    def setUp(self):
        self.env = mock.patch.dict(os.environ, {"FLEET_NOW_LANG": "English"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_the_fields_read_model_state_ctx_now_run_and_the_pane_link_goes(self):
        vm = model([raw_session("f5", state="working", herdr_pane="wX:pA",
                                exec_tool={"name": "pytest", "etime_s": 120})])
        text = body(rows(vm, NavState(scope="session:f5")))
        joined = "\n".join(text)
        for label in ("MODEL", "STATE", "CTX", "NOW", "RUN"):
            self.assertTrue(any(line.startswith("  " + label + " ") for line in text), label)
        for old in ("harness:", "state:", "context:", "now:", "running:", "(open ▸)"):
            self.assertNotIn(old, joined)
        self.assertIn("pane wX:pA  ⏎ go", joined)
        run = next(line for line in text if line.startswith("  RUN "))
        self.assertIn("⚙ pytest · 2m", run)                      # the tool and how long it runs, apart
        frame = draw(vm, NavState(scope="session:f5"), 120, 45, beat=1)
        y = next(i for i, row in enumerate(frame.text_rows()) if "  MODEL " in row)
        self.assertEqual(frame.cells[y][X + 2][1], normalize_style("label")[0])   # dim, like the panel's

    def test_at_80_columns_the_pane_stands_on_its_own_pane_field(self):
        vm = model([raw_session("f5", herdr_pane="wX:pA", model="claude-opus-5-5", effort="xhigh")])
        text = body(rows(vm, NavState(scope="session:f5"), cols=80))
        pane = next(line for line in text if line.startswith("  PANE "))
        self.assertIn("wX:pA", pane)
        self.assertIn("⏎", pane)
        frame = draw(vm, NavState(scope="session:f5"), 80, 45, beat=1)
        self.assertTrue(any(hit.action == ("pane", "f5", "wX:pA") for hit in frame.hits))


class ContextLine(unittest.TestCase):
    """2 · The CTX gauge is ``gauge_parts`` at ten cells; its line is percent · tokens · last clear."""

    def setUp(self):
        self.env = mock.patch.dict(os.environ, {"FLEET_NOW_LANG": "English"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_the_three_forms_ordinary_unknown_and_a_large_long_rest(self):
        from tools.fleet.session_line import LONG_REST, context_line_parts
        text = lambda parts: "".join(part for part, _key in parts)        # noqa: E731
        self.assertEqual(text(context_line_parts(42, 415000, NOW - 3600, None, NOW)),
                         "42% · 415K · cleared " + clock(NOW - 3600))
        self.assertEqual(text(context_line_parts(42, 415000, "04:48", None, NOW)), "42% · 415K · cleared 04:48")
        self.assertEqual(context_line_parts(None, None, None, None, NOW), [])     # nothing is made up
        rest = NOW - LONG_REST[1] - 600
        parts = context_line_parts(81, 812000, "04:48", rest, NOW)
        self.assertEqual(text(parts), "81% · 812K · idle " + clock(rest))       # since when, not how long
        self.assertEqual({key for part, key in parts if part in ("812K", "idle " + clock(rest))}, {"lvl_y"})
        # A short rest or a small context keeps the clear (one constant decides).
        self.assertIn("cleared", text(context_line_parts(81, 812000, "04:48", NOW - 60, NOW)))
        self.assertIn("cleared", text(context_line_parts(30, LONG_REST[0] - 1, "04:48", rest, NOW)))
        with mock.patch.dict(os.environ, {"FLEET_NOW_LANG": "Korean"}):
            self.assertEqual(text(context_line_parts(81, 812000, "04:48", rest, NOW)),
                             "81% · 812K · 쉼 " + clock(rest))
            self.assertEqual(text(context_line_parts(42, None, "04:48", None, NOW)), "42% · 정리 04:48")

    def test_the_page_draws_a_ten_cell_gauge_and_the_line(self):
        vm = model([raw_session("f5", active_context_tokens=812000, mtime=NOW - 2 * 3600)])
        ctx = next(line for line in body(rows(vm, NavState(scope="session:f5"))) if line.startswith("  CTX "))
        self.assertIn("━━━━────── 42% · 812K · idle " + clock(NOW - 2 * 3600), ctx)
        bare = model([raw_session("f5", context_pct=None, window=None)])
        ctx = next(line for line in body(rows(bare, NavState(scope="session:f5"))) if line.startswith("  CTX "))
        self.assertEqual(ctx.split("CTX", 1)[1].strip(), "·")

    def test_the_seat_head_line_carries_generation_and_last_clear(self):
        vm = model([raw_session("f5")]).with_updates(
            seats=(("f5", "hearting", 8, "04:48 · 12m ago", False, "", ()),))
        vm = vm.with_updates(sessions=tuple(dict(row, generation=8, last_clear="04:48") for row in vm.sessions))
        text = body(rows(vm, NavState(scope="session:f5")))
        self.assertIn("  generation 8 · cleared 04:48 · no card body", text)
        self.assertFalse(any("generation" in line for line in text if line.startswith("  CTX ")))

    def test_table_and_header_gauges_draw_through_gauge_parts(self):
        from tools.fleet.explorer.grid import CellGrid
        from tools.fleet.explorer.tables import _draw_context_gauge
        from tools.fleet.session_line import gauge_parts
        for pct, cells in ((40, 5), (81, 10)):
            grid = CellGrid(40, 2)
            _draw_context_gauge(grid, 0, 0, pct, 1.0, narrow=cells == 5)
            drawn = "".join(cell[0] for cell in grid.cells[0][:cells])
            parts = gauge_parts(pct, cells=cells)
            self.assertEqual(drawn, parts[0][0] + parts[1][0])
            self.assertEqual(grid.cells[0][0][1], normalize_style(parts[0][1])[0])


class AnswerWait(unittest.TestCase):
    """3 · ``◑`` is the panel's rose everywhere; ``answer needed`` comes from the word list."""

    def waiting(self):
        session = raw_session("f5", state="blocked", interaction_state={"kind": "question"})
        vm = model([session])
        return vm.with_updates(sessions=tuple(dict(row, answer_wait_marker="◑") for row in vm.sessions))

    def test_the_state_row_and_the_questions_head_wear_rose(self):
        rose = normalize_style("blocked")[0]
        for lang, word, count in (("English", "answer needed", "◑ to answer"), ("Korean", "답 필요", "◑답 대기")):
            with self.subTest(lang=lang), mock.patch.dict(os.environ, {"FLEET_NOW_LANG": lang}):
                frame = draw(self.waiting(), NavState(scope="session:f5"), 120, 45, beat=1)
                text = frame.text_rows()
                y = next(i for i, row in enumerate(text) if "STATE" in row)
                x = text[y].index("◑")
                self.assertEqual(frame.cells[y][x][1], rose)
                self.assertIn("◑ " + word, text[y].replace("  ", " ") if lang == "Korean" else text[y])
                if lang == "Korean":
                    self.assertNotIn("answer needed", "\n".join(text))
                frame = draw(self.waiting(), NavState(view="QUESTIONS", title="QUESTIONS"), 120, 45, beat=1)
                head = next(i for i, row in enumerate(frame.text_rows()) if row[X:].startswith(count[:1]))
                self.assertEqual(frame.cells[head][X][1], rose)
                self.assertNotEqual(frame.cells[head][X][1], normalize_style("lvl_y")[0])   # not yellow 186


class TimeForms(unittest.TestCase):
    """4 · When something happened is a time of day, how long it took a duration; no ``ago``, no seconds."""

    def test_one_function_writes_each_form(self):
        from tools.fleet.session_line import elapsed_text, when_text
        self.assertEqual(when_text(NOW - 600, NOW), clock(NOW - 600))
        self.assertEqual(when_text(NOW - 2 * 86400, NOW), time.strftime("%m-%d", time.localtime(NOW - 2 * 86400)))
        self.assertEqual((elapsed_text(42), elapsed_text(65)), ("42m", "1h 5m"))
        self.assertEqual(when_text(None, NOW), "")
        self.assertEqual(when_text(NOW, "synthetic"), clock(NOW) if time.localtime(NOW)[:3] ==
                         time.localtime()[:3] else time.strftime("%m-%d", time.localtime(NOW)))

    def test_links_messages_and_recent_results_carry_no_ago_or_seconds(self):
        with mock.patch.dict(os.environ, {"FLEET_NOW_LANG": "English"}):
            vm = mesh(sent=[{"to": {"harness": "codex", "session_id": "sid-a3", "name": "a3"},
                             "summary": "please compare", "at": NOW - 90, "kind": "steer",
                             "status": "received", "refs": []}],
                      requests=[request_record()])
            for state in (NavState(scope="session:f5"), NavState(view="PEERS", title="PEERS")):
                joined = "\n".join(body(rows(vm, state, cols=140, lines=60)))
                self.assertNotRegex(joined, r"\bago\b")
                self.assertNotRegex(joined, r"\b\d+s\b")
            links = {str(link[2]): link for link in vm.peer_edges["links"]}
            self.assertEqual(links["watch ⚑"][3], clock(NOW - 8000))
            self.assertEqual(links["REQUEST"][3], clock(NOW - 6000))

    def test_the_repo_page_says_when_a_result_finished(self):
        from tools.fleet.explorer.view_model import ExplorerViewModel
        vm = ExplorerViewModel.from_mapping({
            "clock": NOW, "repos": [{"id": "hearting", "name": "hearting", "done": "the finished work",
                                     "done_at": NOW - 5400, "done_result": "success", "age": "1h 30m ago"}]})
        with mock.patch.dict(os.environ, {"FLEET_NOW_LANG": "English"}):
            text = "\n".join(body(rows(vm, NavState(scope="repo:hearting"), cols=120)))
        self.assertIn(clock(NOW - 5400) + "  ✓ the finished work", text)
        self.assertNotIn("ago", text)


class SectionHeads(unittest.TestCase):
    """5 · A section head inside a screen leads with its count: ``2 ROUTES ───``."""

    def test_heads_lead_with_the_count_and_page_titles_keep_their_row(self):
        vm = model([raw_session("f5")])
        vm = vm.with_updates(routes=tuple({"id": "rt-%d" % n, "repo": "hearting", "session_tag": "f5",
                                           "state": "working", "activity": "running", "title": "route %d" % n,
                                           "stages": [], "age": "5m", "harness": "claude"} for n in (1, 2)))
        with mock.patch.dict(os.environ, {"FLEET_NOW_LANG": "English"}):
            text = body(rows(vm, NavState(scope="session:f5")))
        self.assertTrue(text[4].startswith("── [f5] title f5"))           # the page title row stays
        head = next(line for line in text if "ROUTES" in line)
        self.assertRegex(head, r"^2 ROUTES ─+$")
        self.assertFalse(any(line.startswith("── ROUTES") for line in text))
        self.assertTrue(any(re.fullmatch(r"MESSAGES ─+", line) for line in text))


class RouteResultWord(unittest.TestCase):
    """6 · A failed route's title cell reads ``FAIL · title``; the title gives way, the word stays."""

    def table(self, outcome, cols):
        from tools.fleet.explorer.view_model import ExplorerViewModel
        route = {"id": "rt-1", "repo": "hearting", "session_tag": "f5", "state": "done", "activity": "ended",
                 "outcome": outcome, "title": "words alignment survey", "stages": [], "age": "1h 52m",
                 "harness": "codex"}
        vm = ExplorerViewModel.from_mapping({"sessions": [{"tag": "f5", "harness": "codex", "repo": "hearting",
                                                           "repo_id": "hearting", "state": "idle"}],
                                             "routes": [route], "repos": [{"id": "hearting", "name": "hearting"}]})
        with mock.patch.dict(os.environ, {"FLEET_NOW_LANG": "English"}):
            return next(line for line in body(rows(vm, NavState(view="ROUTES", title="ROUTES"), cols=cols))
                        if "[f5]" in line)

    def test_fail_leads_the_title_and_stays_when_the_cell_is_short(self):
        wide, narrow = self.table("FAIL", 168), self.table("FAIL", 100)
        self.assertRegex(wide, r"FAIL · words")
        self.assertIn("FAIL · ", narrow)
        title = lambda line: re.search(r"FAIL · (\S+(?: \S+)*)", line).group(1)      # noqa: E731
        self.assertLess(len(title(narrow)), len(title(wide)))            # the title gave way, the word stayed
        for passed in ("success", "PASS"):
            self.assertNotIn(passed + " ·", self.table(passed, 168))
            self.assertRegex(self.table(passed, 168), r"  words alignmen")


class SessionMessages(unittest.TestCase):
    """7 · The session page's MESSAGES is the panel's (``peer_message_rows``): no notice, no request."""

    def setUp(self):
        self.env = mock.patch.dict(os.environ, {"FLEET_NOW_LANG": "English"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_messages_leave_out_notices_and_requests_and_count_what_is_drawn(self):
        sent = [{"to": {"harness": "codex", "session_id": "sid-a3", "name": "a3"},
                 "summary": "[요청 rq-0a1b2c] review the popup keys", "at": NOW - 6000, "kind": "request",
                 "status": "received", "refs": ["peer-request:rq-0a1b2c:1:request"]},
                {"to": {"harness": "codex", "session_id": "sid-a3", "name": "a3"}, "summary": "please compare",
                 "at": NOW - 5000, "kind": "steer", "status": "received", "refs": []}]
        received = [{"source": "peer", "sender": "[a3]", "sender_harness": "codex", "sender_session_id": "sid-a3",
                     "text": "[요청 답 rq-0a1b2c] done", "at": NOW - 300, "ref": "b" * 32}]
        vm = mesh(sent=sent, received=received, requests=[request_record()])
        messages = vm.peer_edges["sessions"]["f5"]["messages"]
        self.assertEqual([(m["way"], m["who"], m["body"]) for m in messages],
                         [("←", "[e4]", "compare the session window"), ("→", "[a3]", "please compare")])
        text = body(rows(vm, NavState(scope="session:f5")))
        start = next(i for i, line in enumerate(text) if line.startswith("MESSAGES "))
        self.assertEqual(text[start + 1], "  ← [e4] claude · %s   compare the session window" % clock(NOW - 3 * 3600))
        self.assertEqual(text[start + 2], "  → [a3] codex · %s · steer · delivered   please compare" % clock(NOW - 5000))
        # The older PEERS row still ends the page, as it did: the sessions a link joins to this one, leaving off [09], whom the
        # top line below already names (11-17: the top line is the supervision; this row keeps the message links and the tab link).
        self.assertTrue(text[start + 3].startswith("  PEERS  [a3]   PEERS tab ›"), text[start + 3])
        top = next(i for i, line in enumerate(text) if line.startswith("── [f5]"))
        self.assertRegex(text[top + 1], r"^  PEERS +\[09\] ⚑ ─ \[f5\]   PEERS tab ›")   # it stands under the title, before MODEL
        self.assertTrue(text[top + 2].lstrip().startswith("MODEL"))
        self.assertNotIn("+", text[start])                              # all kept messages are drawn
        joined = "\n".join(text)
        self.assertNotIn("route finished", joined)                      # the runtime notice is no message
        self.assertNotIn("links", joined)

    def test_plus_n_counts_the_kept_messages_the_page_does_not_draw(self):
        sent = [{"to": {"harness": "codex", "session_id": "sid-a3", "name": "a3"}, "summary": "note %d" % n,
                 "at": NOW - 100 * (10 - n), "kind": "steer", "status": "sent", "refs": []} for n in range(9)]
        vm = mesh(sent=sent)
        kept = vm.peer_edges["sessions"]["f5"]["messages"]
        self.assertEqual(len(kept), 8)                                   # the panel keeps eight
        text = body(rows(vm, NavState(scope="session:f5"), lines=60))
        start = next(i for i, line in enumerate(text) if line.startswith("MESSAGES "))
        self.assertIn("+3", text[start])
        drawn = [line for line in text[start + 1:start + 7] if line.startswith("  ← ") or line.startswith("  → ")]
        self.assertEqual(len(drawn) + 3, len(kept))

    def test_the_head_piece_is_one_python_function(self):
        from tools.fleet.session_line import message_head_parts
        parts = message_head_parts({"way": "←", "who": "[e4]", "harness": "claude", "when": "21:02",
                                    "kind": "steer", "delivery": None})
        self.assertEqual("".join(text for text, _key in parts), "← [e4] claude · 21:02 · steer")
        self.assertEqual(parts[0], ("←", "lvl_g"))

    def test_the_peers_link_opens_the_tab_with_this_card_picked(self):
        vm = mesh()
        frame = draw(vm, NavState(scope="session:f5"), 120, 45, beat=1)
        hit = next(hit for hit in frame.hits if hit.action == ("tab", "PEERS", "f5"))
        state, effects = reduce(NavState(scope="session:f5"), {"type": "click", "action": hit.action}, frame)
        self.assertEqual((state.view, state.scope, state.opened_for("pfocus:PEERS")), ("PEERS", "-", "f5"))
        self.assertEqual(effects, ())


class OneRequest(unittest.TestCase):
    """8 · A request and its answer are one ``REQUEST``, counted as the panel counts it."""

    def setUp(self):
        self.env = mock.patch.dict(os.environ, {"FLEET_NOW_LANG": "English"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_links_carry_one_request_line_with_the_panels_state_word(self):
        sessions = [raw_session("f5", peer_last_sent={"to_session_id": "sid-a3", "to_harness": "codex",
                                                      "kind": "request", "age_min": 100}),
                    raw_session("a3", "codex", peer_last_sent={"to_session_id": "sid-f5", "to_harness": "claude",
                                                               "kind": "report", "age_min": 5})]
        for state, word in (("open", "open"), ("partial", "partial answer"), ("final", "final answer")):
            with self.subTest(state=state):
                vm = model(sessions, peer_observation([request_record(state=state)]))
                kinds = [str(link[2]) for link in vm.peer_edges["links"]]
                self.assertEqual(kinds, ["REQUEST"])                     # request and report: one line
                link = vm.peer_edges["links"][0]
                self.assertEqual((link[0][0], link[1][0]), ("f5", "a3"))   # from the requester
                self.assertTrue(str(link[4]).startswith(word), link[4])
                text = "\n".join(body(rows(vm, NavState(view="PEERS", title="PEERS"), cols=140, lines=60)))
                self.assertIn("REQUEST", text)
                self.assertNotIn(" report ", text)

    def test_without_a_record_request_and_report_still_make_one_line(self):
        sessions = [raw_session("f5", peer_last_sent={"to_session_id": "sid-a3", "to_harness": "codex",
                                                      "kind": "request", "age_min": 100}),
                    raw_session("a3", "codex", peer_last_sent={"to_session_id": "sid-f5", "to_harness": "claude",
                                                               "kind": "report", "age_min": 5})]
        vm = model(sessions)
        self.assertEqual([(str(link[2]), link[0][0], link[1][0]) for link in vm.peer_edges["links"]],
                         [("REQUEST", "f5", "a3")])

    def test_the_session_head_counts_requests_as_the_panel_does_and_lists_the_line(self):
        from tools.fleet.session_panel import request_rows
        record = request_record(state="final", closed=NOW - 300)
        vm = mesh(requests=[record])
        mine = vm.peer_edges["sessions"]["f5"]["requests"]
        views = [{"record": record, "side": "out", "gone": False, "body": "",
                  "rows": {"a" * 32: {"state": "received"}, "b" * 32: {"state": "received"}}}]
        panel = request_rows(views, NOW, lambda harness, sid: {"sid-a3": "a3"}.get(sid))
        self.assertEqual(len(mine), len(panel))
        self.assertEqual([row["status"] for row in mine], [row["status"] for row in panel])
        text = body(rows(vm, NavState(scope="session:f5")))
        self.assertTrue(any(re.fullmatch(r"ROUTES ─+", line) for line in text), text)    # the head no longer counts requests
        self.assertTrue(any(re.fullmatch(r" ─ request ─+", line) for line in text), text)   # they stand under a named rule
        self.assertFalse(any("PEER REQUEST" in line for line in text), text)
        line = next(line for line in text if "→ [a3] codex" in line)
        self.assertIn("✓ → [a3] codex · %s · final answer %s   review the popup keys"
                      % (clock(NOW - 6000), clock(NOW - 300)), line)
        # Closed long ago and seen, a request leaves the session page as it leaves the panel.
        old = mesh(requests=[request_record(state="final", closed=NOW - 3600)])
        self.assertFalse(old.peer_edges["sessions"]["f5"]["requests"])
        self.assertEqual(request_rows([dict(views[0], record=request_record(state="final", closed=NOW - 3600))],
                                      NOW, lambda harness, sid: None), [])


class PeersCount(unittest.TestCase):
    """9 · PEERS counts linked sessions; a runtime notice is no link; the words are the panel's."""

    def setUp(self):
        self.env = mock.patch.dict(os.environ, {"FLEET_NOW_LANG": "English"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_the_tab_counts_sessions_and_links_end_in_two_sessions(self):
        sessions = [raw_session("f5", peer_last_recv={"from_session_id": "sid-e4", "from_harness": "claude",
                                                      "kind": "steer", "age_min": 30},
                                peer_last_sent={"to_session_id": "sid-a3", "to_harness": "codex",
                                                "kind": "request", "age_min": 100}),
                    raw_session("e4", state="working"), raw_session("a3", "codex"),
                    raw_session("09", steward=True, steward_targets=[
                        {"harness": "claude", "session_id": "sid-f5", "name": "f5", "kind": "watch",
                         "ts": iso(NOW - 8000), "source": "watch"}])]
        notice = {"from": {"harness": "runtime", "session_id": None, "name": "Hearting runtime"},
                  "to": {"harness": "claude", "session_id": "sid-f5"}, "kind": "notice",
                  "summary": "route finished", "ts": iso(NOW - 2640), "delivery": {"status": "received"}}
        vm = model(sessions, peer_observation([request_record()]),
                   extra={"observations": {"optional:peers": {"state": "success", "observed_at": NOW,
                          "messages": peer_observation([request_record()])},
                          "replay_sources": {"peer_messages": {"records": [notice]}}}})
        links = vm.peer_edges["links"]
        self.assertEqual(sorted(str(link[2]) for link in links), ["REQUEST", "steer", "watch ⚑"])
        self.assertTrue(all(isinstance(end, (list, tuple)) for link in links for end in link[:2]))
        frame = draw(vm, NavState(view="PEERS", title="PEERS"), 140, 60, beat=1)
        text = frame.text_rows()
        peers_tab = next(row for row in text if row.startswith(" PEERS "))
        self.assertEqual(peers_tab[:32].split()[-1], "4")                # f5 e4 a3 09, not three LINKS rows
        self.assertTrue(any(re.match(r"3 LINKS messages·requests · supervision · handover ─", row[X:]) for row in text))
        self.assertTrue(any("steward ⚑" in row for row in text))
        self.assertFalse(any("watch ⚑" in row[X:] and "→" in row for row in text))
        self.assertIn("PEERS  4 sessions · 3 links · ", text[-1])
        self.assertNotIn("runtime", "\n".join(row[X:] for row in text))
        with mock.patch.dict(os.environ, {"FLEET_NOW_LANG": "Korean"}):
            korean = draw(vm, NavState(view="PEERS", title="PEERS"), 140, 60, beat=1).text_rows()
        self.assertTrue(any("감독 ⚑" in row for row in korean))
        self.assertIn("4 세션", korean[-1])
        self.assertEqual(peers.link_kind_text("watch ⚑"), "steward ⚑")

    def test_a_runtime_notice_still_reaches_the_feed_line(self):
        notice = {"from": {"harness": "runtime", "session_id": None, "name": "Hearting runtime"},
                  "to": {"harness": "claude", "session_id": "sid-f5"}, "kind": "notice",
                  "summary": "route finished · words survey FAIL", "ts": iso(NOW - 120),
                  "delivery": {"status": "received"}}
        vm = model([raw_session("f5")], extra={"observations": {"optional:peers": {"state": "success",
                   "observed_at": NOW}, "replay_sources": {"peer_messages": {"records": [notice]}}}})
        self.assertFalse(vm.peer_edges.get("links"))
        self.assertTrue(any("route finished" in str(event[-1]) for event in vm.events))


class PeerRequestRead(unittest.TestCase):
    """The peer observation reads the request records and the session's sent records, writing nothing."""

    def test_requests_and_sent_records_are_read_without_a_write(self):
        from tools.fleet import details
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR") or None) as root:
            store = Path(root) / "peer-messages" / "requests"
            store.mkdir(parents=True, mode=0o700)
            os.chmod(Path(root) / "peer-messages", 0o700)
            record = request_record(state="open")
            fd = os.open(store / (record["id"] + ".json"), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(record, handle)
            before = sorted(os.listdir(store))
            sessions = [SimpleNamespace(harness="claude", session_id="sid-f5", session_aliases=()),
                        SimpleNamespace(harness="codex", session_id="sid-a3", session_aliases=())]
            sent = [{"from": {"harness": "claude", "session_id": "sid-f5"}, "to": {"harness": "codex",
                     "session_id": "sid-a3"}, "summary": "please compare", "ts": "2026-11-16T12:00:00+00:00",
                     "kind": "steer", "message_id": "m1", "delivery": {"status": "sent"}},
                    {"from": {"harness": "claude", "session_id": "sid-f5"}, "to": {"harness": "codex",
                     "session_id": "sid-a3"}, "summary": "please compare", "ts": "2026-11-16T12:01:00+00:00",
                     "kind": "steer", "message_id": "m1", "delivery": {"status": "received"}}]
            with mock.patch.dict(os.environ, {"AGENT_PEER_LEDGER_ROOT": root}):
                observed = details._peer_requests(sessions, sent, now=NOW)
            self.assertEqual(sorted(os.listdir(store)), before)          # no index, no settle written
            self.assertEqual(list(observed["requests"]), [record["id"]])
            self.assertEqual(observed["sides"], {"claude:sid-f5": [[record["id"], "out"]],
                                                 "codex:sid-a3": [[record["id"], "in"]]})
            self.assertIn("a" * 32, observed["refs"])
            self.assertEqual([(row["summary"], row["status"]) for row in observed["sent"]["claude:sid-f5"]],
                             [("please compare", "received")])           # a later record updates its status


if __name__ == "__main__":
    unittest.main()
