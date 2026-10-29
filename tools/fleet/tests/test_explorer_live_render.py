"""Renderer rules for live view models, each reproduced on a small scrubbed synthetic view model."""
import contextlib
import io
import os
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tools.fleet.explorer.frame import draw
from tools.fleet.explorer.resources import short_cmd
from tools.fleet.explorer.state import NavState
from tools.fleet.explorer.system import usage_text
from tools.fleet.explorer.tables import _harness_mix, _stage_hue, _stage_items, gauge_cells
from tools.fleet.explorer.view_model import ExplorerViewModel, load_fixture

FIXTURES = Path(__file__).parent / "fixtures" / "explorer"


def _mapping():
    return load_fixture(FIXTURES / "view_model.json").to_mapping()


def _vm(**changes):
    values = _mapping()
    values.update(changes)
    return ExplorerViewModel.from_mapping(values)


def _frame(vm, view="OVERVIEW", cols=140, rows=60, scope="-"):
    return draw(vm, NavState(view=view, title=view, scope=scope), cols, rows, beat=1)


def _find(frame, text, line=""):
    """(row, column) of the first cell run that spells ``text`` on a row that also holds ``line``."""
    for y, row in enumerate(frame.text_rows()):
        x = row.find(text)
        if x >= 0 and line in row:
            return y, x
    raise AssertionError(f"{text!r} is not drawn")


def _key(frame, text, line=""):
    y, x = _find(frame, text, line)
    return frame.cells[y][x][1]


def _route(**changes):
    route = dict(_mapping()["routes"][1])
    route.update(changes)
    return route


class StageRule(unittest.TestCase):
    def test_worker_step_takes_the_hue_of_its_index_in_the_route_stages(self):
        stages = [{"name": name, "st": "done"} for name in ("alpha", "bravo", "charlie")] + [
            {"name": "delta-step", "st": "active"}, {"name": "echo", "st": "todo"}]
        base = dict(stages=stages, worker_stage="delta-step", worker_stage_key=None, worker_step_key=None,
                    worker=["worker", [["codex", "hb_codex"]], [["running 3m", "dim"]]],
                    activity="running", state="working")
        self.assertEqual(_stage_hue({"stages": stages}, "delta-step"), "y")
        self.assertEqual(_stage_hue({"stages": stages}, "not-a-stage"), "g")
        running = _frame(_vm(routes=[_route(**base)]), "ROUTES")
        self.assertEqual(_key(running, "delta-step", "└ worker"), "g_idle")        # (yellow, normal weight) while it runs
        self.assertEqual(_key(running, "running 3m", "└ worker"), "stg3_on")       # (yellow, bold)
        parked = dict(base, worker=["worker", [["codex", "hb_codex"]], [["3m · worker ended", "dim"]]])
        self.assertEqual(_key(_frame(_vm(routes=[_route(**parked)]), "ROUTES"), "delta-step", "└ worker"), "lvl_y_dim")

    def test_every_stage_index_has_a_hue(self):
        items = _stage_items([{"name": f"s{index}", "st": "active"} for index in range(6)])
        keys = [parts[0][1] for parts, _status in items]
        self.assertNotIn("unknown", keys)
        self.assertEqual(keys[:2], ["hb_opencode", "hb_claude"])

    def test_harness_mix_counts_only_drawn_depth2_workers(self):
        base = dict(state="working", harness="claude",
                    worker=["worker", [["codex", "hb_codex"]], [["3m", "dim"]]])

        def text(**changes):
            return "".join(part for part, _key in _harness_mix([_route(**base, **changes)]))
        self.assertIn("claude 1 · codex 0 · opencode 0", text(subworker=None))
        self.assertIn("claude 1 · codex 1 · opencode 0", text(subworker=("execute", "codex", "running")))

    def test_worker_effort_max_is_kept_in_the_folded_cell(self):
        worker = ["worker", [["codex", "hb_codex"], [" (", "dim"], ["gpt-x", "fam_gpt"], ["·", "dim"],
                             ["max", "dead"], [")", "dim"]], [["3m", "dim"]]]
        frame = _frame(_vm(routes=[_route(worker=worker)]), "ROUTES", cols=100)
        self.assertIn("codex·max", "\n".join(frame.text_rows()))


class GaugeRule(unittest.TestCase):
    def test_a_value_above_zero_fills_at_least_one_cell(self):
        self.assertEqual([gauge_cells(6, value) for value in (0, 1, 6, 50, 99, 100)], [0, 1, 1, 3, 5, 6])

    def test_overview_context_gauge_shows_a_small_percentage(self):
        sessions = [dict(session, context=6, window=0.75) for session in _mapping()["sessions"]]
        frame = _frame(_vm(sessions=sessions))
        self.assertIn("━─────", "\n".join(frame.text_rows()))


class CommandRule(unittest.TestCase):
    def test_process_command_keeps_the_script_and_short_options(self):
        self.assertEqual(short_cmd("python -B /srv/work/resume_x.py --request /srv/in/A2.json"),
                         "resume_x.py --request A2.json")
        self.assertEqual(short_cmd("python x.py --request-sha256 0123456789abcdef0123 --seed 7"),
                         "x.py --seed 7")
        self.assertEqual(short_cmd("python x.py --sha=0123456789abcdef0123"), "x.py")
        self.assertEqual(short_cmd("tool --flag"), "tool --flag")


class HeaderRule(unittest.TestCase):
    def test_answered_head_counts_the_first_repos_own_rows(self):
        answered = (("repo-a", (("10:00", "one"), ("10:01", "two"))), ("repo-b", (("09:00", "three"),)))
        system = dict(_mapping()["system"], answered_total=3)
        frame = _frame(_vm(answered=answered, system=system), "QUESTIONS")
        self.assertIn("repo-a 2 · 3 decision records in all projects", "\n".join(frame.text_rows()))


class CutRule(unittest.TestCase):
    def test_route_header_uses_the_cell_before_age_without_overlapping_it(self):
        for cols in (80, 100, 140, 168):
            with self.subTest(cols=cols):
                rows = _frame(_vm(), "ROUTES", cols=cols).text_rows()
                header = next(row[34:] for row in rows if "REPO" in row and "ROUTE" in row)
                labels = ("REPO", "ID", "ROUTE", "KIND", "HARNESS",
                          *(("PROGRESS",) if "PROGRESS" in header else ()), "AGE")
                positions = [header.index(label) for label in labels]
                self.assertEqual(positions, sorted(positions))
                for position, label in zip(positions, labels):
                    self.assertEqual(header[position:position + len(label)], label)
                for index in range(len(labels) - 1):
                    self.assertLess(positions[index] + len(labels[index]), positions[index + 1])

    def test_route_kind_uses_the_cell_before_age_instead_of_dropping_its_last_glyph(self):
        route = _route(capability="lab", mode="eval", intensity="standard", age="22h 32m")
        rows = _frame(_vm(routes=[route], repos=[]), "ROUTES", cols=80).text_rows()
        line = next(row for row in rows if "lab(eval" in row)
        kind = line.index("lab(eval")
        age = line.index("22h 32m")
        self.assertEqual(line[kind:kind + 8], "lab(eval")
        self.assertLess(kind + 8, age)
        self.assertEqual(line[age:age + 7], "22h 32m")
        # REPO now precedes ID; the 80-column pane shares the remaining cells.
        owner = str(route.get("harness"))
        self.assertIn(owner[:5], line)

    def test_system_field_lines_are_cut_at_the_pane_edge_without_an_ellipsis(self):
        system = dict(_mapping()["system"], install={"version": "v1.2.3", "managed": True, "pinned": True,
                                                       "distribution": True},
                      usage_api={"state": "on", "freshness": "9m ago"})
        rows = _frame(_vm(system=system), "SYSTEM", cols=80).text_rows()
        field = next(row for row in rows if "version:" in row)
        self.assertNotIn("…", field)

    def test_overview_summary_is_cut_at_the_screen_edge_without_an_ellipsis(self):
        rows = _frame(_vm(), "OVERVIEW", cols=80).text_rows()
        summary = next(row for row in rows if " sessions · " in row and "working" in row)
        self.assertNotIn("…", summary)

    def test_session_context_line_keeps_last_clear_when_the_time_is_unknown(self):
        sessions = [dict(session, last_clear=None) for session in _mapping()["sessions"]]
        tag = next(seat[0] for seat in _mapping()["seats"])
        frame = _frame(_vm(sessions=sessions), "OVERVIEW", cols=168, scope="session:" + tag)
        context = next(row for row in frame.text_rows() if "context:" in row)
        self.assertIn("· last clear ·", context)


class ModelsRule(unittest.TestCase):
    def _models(self, **changes):
        return dict(_mapping()["models"], **changes)

    def test_main_only_models_print_their_names_not_a_tuple(self):
        models = self._models(main_only={"claude": ("fable",), "codex": ["gpt-6-astra", "other-model"]})
        text = "\n".join(_frame(_vm(models=models), "MODELS").text_rows())
        self.assertIn("claude Fable 5.1  ·  codex gpt-6-astra  ·  codex other-model", text)
        self.assertNotIn("('", text)

    def test_missing_tidy_points_draw_the_configured_bar_not_a_red_one(self):
        def bar_keys(models):
            frame = _frame(_vm(models=models), "MODELS")
            y, x = _find(frame, "claude")
            y = next(index for index, row in enumerate(frame.text_rows())
                     if index > y and row[34:].lstrip().startswith("claude") and "━" in row)
            return [cell[1] for cell in frame.cells[y] if cell[0] == "━"]
        configured = bar_keys(self._models(tidy=[500000, 700000]))
        self.assertEqual(bar_keys(self._models(tidy=None)), configured)
        self.assertIn("g_idle", configured)
        self.assertNotEqual(set(configured), {"g_blocked"})


class PeersRule(unittest.TestCase):
    def test_unknown_context_draws_no_gauge_on_the_card(self):
        sessions = [dict(session, context=None) for session in _mapping()["sessions"]]
        frame = _frame(_vm(sessions=sessions), "PEERS")
        self.assertNotIn("━", "\n".join(row[34:] for row in frame.text_rows()[5:]))


class UsageRule(unittest.TestCase):
    def test_unknown_usage_is_a_dot(self):
        self.assertEqual(usage_text(SimpleNamespace(usage_snapshots={}, sessions=())), "·")


class ClaudeUsageStatus(unittest.TestCase):
    NOW = 2_000_000_000.0

    def vm(self, snapshot, sessions=()):
        from tools.fleet.explorer.live import build_view_model
        return build_view_model({"sessions": sessions, "usage_snapshots": {"claude": snapshot}},
                                cached_config={}, now=self.NOW)

    def observation(self, error="rate-limit", **changes):
        reason = {"rate-limit": "usage-rate-limited", "auth": "authentication-rejected",
                  "missing-credential": "credential-not-found", "unknown": "usage-unreadable"}.get(error)
        return {"payload": None, "freshness": "unknown", "observed_at": None,
                "error_observation": {"error": error, "reason": reason,
                                      "observed_at": self.NOW - 60, **changes}}

    def test_no_quota_and_sanitized_errors_remain_visible_in_both_modes(self):
        cases = [({"payload": None, "freshness": "unknown"}, "unavailable")]
        cases += [(self.observation(error), label) for error, label in (
            ("rate-limit", "rate limit"), ("auth", "auth"),
            ("missing-credential", "no credential"), ("unknown", "unavailable"))]
        cases.append((self.observation("unknown", reason="usage-rate-limited"), "rate limit"))
        cases.append(({"payload": {"rl_7d": float("nan")}}, "unavailable"))
        for snapshot, label in cases:
            vm = self.vm(snapshot)
            self.assertEqual(len(vm.usage), 1)
            self.assertFalse(any(vm.usage[0]["windows"]))
            for expanded in (False, True):
                with self.subTest(label=label, expanded=expanded):
                    frame = draw(vm, NavState(usage_open=expanded), 140, 45, beat=1)
                    row = frame.text_rows()[1]
                    self.assertIn("claude", row)
                    self.assertIn(label, row)
                    self.assertNotIn("%", row)
                    self.assertNotIn("━", row)
                    self.assertNotIn("─", row)

    def test_authoritative_unknown_or_error_never_borrows_session_quota(self):
        sessions = [{"harness": "claude", "rl_5h": 97, "rl_7d": 98, "mtime": self.NOW}]
        for snapshot in ({}, None, {"freshness": "unknown", "payload": {"rl_7d": 98}},
                         self.observation("auth"), self.observation()):
            with self.subTest(snapshot=snapshot):
                vm = self.vm(snapshot, sessions)
                self.assertFalse(any(vm.usage[0]["windows"]))
                self.assertNotIn("98", vm.usage[0]["text"])

    def test_absent_backend_entry_excludes_claude_fallback_and_preserves_other_harnesses(self):
        from tools.fleet.explorer.live import build_view_model
        sessions = [{"harness": harness, "rl_5h": 0, "rl_7d": 33}
                    for harness in ("claude", "codex", "opencode")]
        for snapshots in ({}, {"claude": self.observation("auth")}):
            vm = build_view_model({"sessions": sessions, "usage_snapshots": snapshots},
                                  cached_config={}, now=self.NOW)
            self.assertEqual(len(vm.usage), 3)
            for row in vm.usage:
                if row.get("key") == "hb_claude":
                    self.assertFalse(any(row["windows"]))
                    continue
                self.assertEqual(row["windows"][0], ("5h", 0, None))
                self.assertEqual(row["windows"][1], ("week", 33, None))

    def test_long_retry_survives_expired_quota_and_keeps_original_observation_age(self):
        from tools.fleet.explorer.live import build_view_model
        from tools.fleet.explorer.live import _clock
        for delay in (7200, 90000):
            snapshot = self.observation(observed_at=self.NOW - 1800, retry_at=self.NOW + delay)
            for elapsed, age in ((0, "30m ago"), (900, "45m ago")):
                vm = build_view_model({"sessions": [], "usage_snapshots": {"claude": snapshot}},
                                      cached_config={}, now=self.NOW + elapsed)
                self.assertIn("wait", vm.usage[0]["text"])
                self.assertIn(_clock(self.NOW + delay), vm.usage[0]["text"])
                self.assertIn(age, vm.usage[0]["text"])
                if delay > 86400:
                    self.assertIn("1d", vm.usage[0]["text"])
                self.assertFalse(any(vm.usage[0]["windows"]))

    def test_expired_missing_invalid_retry_or_observation_never_becomes_a_live_wait(self):
        for retry in (None, self.NOW, self.NOW - 1, True, "tomorrow", float("nan"), float("inf"), 1e20):
            with self.subTest(retry=retry):
                text = self.vm(self.observation(retry_at=retry)).usage[0]["text"]
                self.assertNotIn("wait", text)
                self.assertNotIn("00:00", text)
        for observed in (None, True, self.NOW + 1, "now", -1, float("nan")):
            with self.subTest(observed=observed):
                text = self.vm(self.observation(observed_at=observed, retry_at=self.NOW + 7200)).usage[0]["text"]
                self.assertNotIn("wait", text)

    def test_numeric_fresh_success_preserves_known_zero_and_stale_uses_original_age(self):
        snapshot = self.observation(retry_at=self.NOW + 7200)
        snapshot.update(payload={"rl_5h": 0, "rl_7d": 63, "rs_7d": self.NOW + 3600},
                        freshness="fresh", observed_at=self.NOW)
        vm = self.vm(snapshot, [{"harness": "claude", "rl_7d": 99}])
        self.assertEqual(vm.usage[0]["text"], "claude")
        self.assertEqual(vm.usage[0]["windows"], (("5h", 0, None), ("week", 63, "1h 0m"), None))
        snapshot.update(freshness="stale", observed_at=self.NOW - 200, attempted_at=self.NOW)
        vm = self.vm(snapshot)
        self.assertIn("stale", vm.usage[0]["text"])
        self.assertIn("3m ago", vm.usage[0]["text"])
        self.assertIn("wait", vm.usage[0]["text"])
        self.assertEqual(vm.usage[0]["windows"], (("5h", 0, None), ("week", 63, "1h 0m"), None))

    def test_error_details_and_account_scope_do_not_enter_display_or_persistence(self):
        marker = "private-fixture-value"
        snapshot = self.observation(error=marker, reason=marker, account_scope=marker,
                                    body=marker, retry_at=self.NOW + 3600)
        vm = self.vm(snapshot)
        self.assertNotIn(marker, str(vm.to_mapping()["usage"]))
        self.assertIn("unavailable", vm.usage[0]["text"])
        self.assertNotIn("wait", vm.usage[0]["text"])
        from tools.fleet.explorer.view_model import persistable_view_model
        persisted = persistable_view_model(vm)
        self.assertNotIn(marker, str(persisted))

    def test_status_and_retry_fit_narrow_headers_and_keep_usage_toggle_hits(self):
        from tools.fleet.explorer.grid import cell_width
        from tools.fleet.explorer.state import reduce
        vm = self.vm(self.observation(observed_at=self.NOW - 1800, retry_at=self.NOW + 7200))
        for width in (40, 60, 80, 100, 140, 180, 240, 320):
            with self.subTest(width=width):
                state = NavState()
                for expanded in (False, True):
                    frame = draw(vm, state, width, 45, beat=1)
                    self.assertEqual(state.usage_open, expanded)
                    self.assertEqual(cell_width(frame.text_rows()[1]), width)
                    self.assertIn("claude wait", frame.text_rows()[1])
                    self.assertIn("less" if expanded else "more", frame.text_rows()[1])
                    hit = next(hit for hit in frame.hits if hit.action == ("usage",))
                    self.assertTrue(0 <= hit.x0 < hit.x1 <= width)
                    self.assertEqual(hit.y, 1)
                    state, effects = reduce(state, {"type": "click", "action": hit.action}, frame)
                    self.assertFalse(effects)
                self.assertFalse(state.usage_open)
                state, effects = reduce(state, {"type": "key", "key": "u"})
                self.assertTrue(state.usage_open)
                self.assertFalse(effects)


class ClaudeActualContract(unittest.TestCase):
    # Verbatim public examples from the backend handoff; all epochs/values are fake.
    SUCCESS = {"payload": {"rl_5h": 27, "rl_7d": 41, "rl_ms": [], "rs_5h": None, "rs_7d": None},
               "freshness": "fresh", "observed_at": 10000.0, "attempted_at": 10000.0,
               "error_observation": None}
    STALE = {"payload": {"rl_5h": 27, "rl_7d": 41, "rl_ms": [], "rs_5h": None, "rs_7d": None},
             "freshness": "stale", "observed_at": 10000.0, "attempted_at": 10200.0,
             "error_observation": {"error": "rate-limit", "reason": "usage-rate-limited",
                                   "observed_at": 10200.0, "account_scope": "current-credential",
                                   "retry_at": 13000.0}}
    EXPIRED = {"payload": None, "freshness": "unknown", "observed_at": None, "attempted_at": 10200.0,
               "error_observation": STALE["error_observation"]}
    CHANGED = {"payload": None, "freshness": "unknown", "observed_at": None, "attempted_at": None,
               "error_observation": {"error": "unknown", "reason": "credential-changed",
                                     "observed_at": 12000.0, "account_scope": "current-credential"}}

    def vm(self, entry, now=10000, **fields):
        from tools.fleet.explorer.live import build_view_model
        return build_view_model({"usage_snapshots": {"claude": entry}, **fields},
                                cached_config={}, now=now)

    def test_fixed_success_stale_rate_limit_expired_long_retry_and_credential_change(self):
        for entry, now, status in ((self.SUCCESS, 10000, None), (self.STALE, 10200, "stale"),
                                   (self.EXPIRED, 12000, "wait"), (self.CHANGED, 12000, "unavailable")):
            with self.subTest(status=status):
                row = self.vm(entry, now).usage[0]
                if status is None:
                    self.assertEqual(row["windows"], (("5h", 27, None), ("week", 41, None), None))
                    continue
                self.assertIn(status, row["text"])
                self.assertEqual(any(row["windows"]), entry is self.STALE)
                self.assertNotIn("exhaust", row["text"])
                if entry is self.STALE:
                    self.assertIn("3m ago", row["text"])
                    self.assertIn("wait", row["text"])
                    self.assertNotIn("0m ago", row["text"])
                if entry is self.EXPIRED:
                    self.assertIn("30m ago", row["text"])
                if entry is self.CHANGED:
                    self.assertNotIn("wait", row["text"])

    def test_all_session_quota_shapes_are_excluded_when_snapshot_or_entry_is_absent(self):
        from tools.fleet.explorer.live import build_view_model
        from tools.fleet import render
        from tools.fleet.model import Session
        shapes = ({"rl_5h": 97}, {"rl_7d": 98}, {"rl_ms": [["opus", 96]]},
                  {"rl_windows": [["week", 95, None]]}, {"rl_rs": [11000, 12000]},
                  {"rl_5h": 97, "rl_7d": 98, "rl_ms": [["opus", 96]],
                   "rl_windows": [["week", 95, None]], "rl_rs": [11000, 12000]})
        for shape in shapes:
            for snapshots in (None, {}, {"codex": {}}, {"claude": None}, {"claude": {}},
                              {"claude": {"freshness": "unknown", "payload": {"rl_7d": 99}}},
                              {"claude": {"payload": {"rl_7d": "bad"}}}, {"claude": "bad"}):
                with self.subTest(shape=shape, snapshots=snapshots):
                    session = {"harness": "claude", "mtime": 10000, **shape}
                    fields = {"sessions": [session]}
                    if snapshots is not None:
                        fields["usage_snapshots"] = snapshots
                    vm = build_view_model(fields, cached_config={}, now=10000)
                    self.assertFalse(any(window for row in vm.usage for window in row["windows"]))
                    legacy = render._usage_header_rows([Session(pid=12345, **session)], now=10000,
                                                       usage_snapshots=snapshots)
                    text = "".join(text for row in legacy for text, _key in row)
                    self.assertIn("unavailable", text)
                    self.assertNotIn("%", text)

    def test_malformed_unknown_and_nonpublic_windows_do_not_admit_quota(self):
        from tools.fleet.explorer.system import usage_text
        for entry in (None, [], "bad", {}, {"payload": None, "error_observation": None},
                      {**self.SUCCESS, "freshness": "unknown"},
                      {**self.SUCCESS, "observed_at": None}, {**self.SUCCESS, "observed_at": 10001},
                      {**self.SUCCESS, "payload": {"windows": [["week", 99, None]]}},
                      {**self.SUCCESS, "payload": {"rl_windows": [["week", 99, None]]}}):
            with self.subTest(entry=entry):
                self.assertFalse(any(self.vm(entry).usage[0]["windows"]))
                self.assertNotIn("%", usage_text({"usage_snapshots": {"claude": entry}}))
        for value in (True, "27", -1, 101, 2.5, float("nan"), float("inf"), {}, []):
            for payload in ({"rl_5h": value}, {"rl_7d": value}, {"rl_ms": [["opus", value]]}):
                with self.subTest(payload=payload):
                    self.assertFalse(any(self.vm({**self.SUCCESS, "payload": payload}).usage[0]["windows"]))
        for models in (3, "opus", [None], [["opus"]], [["opus", 1, 2]], [[{}, 1]]):
            self.assertFalse(any(self.vm({**self.SUCCESS, "payload": {"rl_ms": models}}).usage[0]["windows"]))
        for observed in (True, -1, "now", float("nan"), float("inf")):
            self.assertFalse(any(self.vm({**self.SUCCESS, "observed_at": observed}).usage[0]["windows"]))

    def test_actual_error_enums_and_reason_allowlist_null_are_sanitized(self):
        reasons = {"auth": "authentication-rejected", "rate-limit": "usage-rate-limited",
                   "timeout": "usage-timeout", "network": "usage-unreachable",
                   "response-invalid": "usage-response-invalid", "missing-credential": "credential-not-found",
                   "unknown": "usage-unreadable"}
        for error, reason in reasons.items():
            for safe_reason in (reason, None, "private-fixture-value", "rate-limit"):
                observation = {"error": error, "reason": safe_reason, "observed_at": 10000,
                               "account_scope": "current-credential", "retry_at": 13000}
                row = self.vm({**self.EXPIRED, "error_observation": observation}, 12000).usage[0]
                self.assertIn("wait", row["text"])
                self.assertNotIn("private-fixture-value", row["text"])
                self.assertNotIn("exhaust", row["text"])
        for reason in ("credential-invalid", "credential-store-read-failed", "credential-changed",
                       "usage-http-error", "usage-unreadable"):
            observation = {"error": "unknown", "reason": reason, "observed_at": 12000}
            self.assertIn("unavailable", self.vm({**self.EXPIRED, "error_observation": observation}, 12000).usage[0]["text"])
        self.assertIn("unavailable", self.vm({**self.EXPIRED, "error_observation": None}, 12000).usage[0]["text"])
        row = self.vm({**self.EXPIRED, "error_observation": {"error": "not-an-enum",
                      "reason": "usage-rate-limited", "observed_at": 10000, "retry_at": 13000}}, 12000).usage[0]
        self.assertNotIn("wait", row["text"])

    def test_quota_age_boundaries_and_attempt_time_never_refresh_observed_quota(self):
        for age, label in ((0, "claude"), (180, "claude"), (181, "stale"), (900, "stale"), (901, "unavailable")):
            row = self.vm({**self.SUCCESS, "attempted_at": 10000 + age}, 10000 + age).usage[0]
            self.assertIn(label, row["text"])
            self.assertEqual(any(row["windows"]), age <= 900)

    def test_current_error_retry_remains_visible_while_last_good_quota_is_young(self):
        from tools.fleet import render
        for error in ("rate-limit", "auth", "timeout", "network", "response-invalid", "unknown", "missing-credential"):
            entry = {**self.SUCCESS, "attempted_at": 10060, "error_observation": {
                "error": error, "reason": None, "observed_at": 10060, "retry_at": 13000}}
            vm = self.vm(entry, 10060)
            self.assertIn("wait", vm.usage[0]["text"])
            self.assertIn("fresh", vm.usage[0]["text"])
            self.assertIn("1m ago", vm.usage[0]["text"])
            self.assertEqual(vm.usage[0]["windows"], (("5h", 27, None), ("week", 41, None), None))
            legacy = render._usage_header_rows([], now=10060, usage_snapshots={"claude": entry})
            text = "".join(text for row in legacy for text, _key in row)
            self.assertIn("wait", text)
            self.assertIn("27%", text)
            self.assertIn("41%", text)
            entry["error_observation"]["observed_at"] = 9999
            self.assertEqual(self.vm(entry, 10060).usage[0]["text"], "claude")

    def test_last_good_zero_partial_windows_and_resets_survive_errors_until_expiry(self):
        from tools.fleet import render
        from tools.fleet.explorer.live import _clock
        from tools.fleet.explorer.system import usage_text
        from tools.fleet.explorer.view_model import persistable_view_model
        for payload in ({"rl_5h": 0, "rl_7d": 0}, {"rl_7d": 0},
                        {"rl_ms": [["opus", 0]]}, {"rl_5h": 27, "rl_7d": 41, "rl_ms": [["opus", 88]]}):
            for error in (None, self.STALE["error_observation"],
                          {"error": "timeout", "reason": "private-fixture-value", "observed_at": 10200}):
                with self.subTest(payload=payload, error=error):
                    entry = {**self.STALE, "payload": {**payload, "rs_5h": 13800, "rs_7d": 17400},
                             "error_observation": error}
                    row = self.vm(entry, 10200).usage[0]
                    self.assertEqual([window[1] for window in row["windows"] if window],
                                     [payload[key] for key in ("rl_5h", "rl_7d") if key in payload]
                                     + [value for _label, value in payload.get("rl_ms", [])])
                    self.assertIn("stale", row["text"])
                    self.assertIn("3m ago", row["text"])
                    if "rl_5h" in payload:
                        self.assertEqual(row["windows"][0][2], "1h 0m")
                    if "rl_7d" in payload:
                        self.assertEqual(row["windows"][1][2], "2h 0m")
                    legacy = render._usage_header_rows([], now=10200, usage_snapshots={"claude": entry})
                    text = "".join(text for line in legacy for text, _key in line)
                    self.assertIn("%", text)
                    self.assertIn("stale", text)
                    if error and error.get("retry_at"):
                        self.assertIn("API wait " + _clock(13000), text)
                    if error and error["error"] == "timeout":
                        self.assertIn("timeout", row["text"])
                    if "rl_5h" in payload or "rl_7d" in payload:
                        with patch("time.time", return_value=10200):
                            summary = usage_text({"usage_snapshots": {"claude": entry}})
                        self.assertIn("%", summary)
                        self.assertIn("stale", summary)
                        self.assertIn("3m ago", summary)
                    frame = draw(self.vm(entry, 10200), NavState(usage_open=True), 180, 45, beat=1)
                    self.assertIn("0%" if 0 in payload.values() or payload.get("rl_ms") == [["opus", 0]] else "27%",
                                  frame.text_rows()[1])
                    self.assertIn("·", frame.text_rows()[1])
                    self.assertNotIn("private-fixture-value", repr(persistable_view_model(self.vm(entry, 10200))))
                    for unusable in ({**entry, "payload": None}, {**entry, "freshness": "unknown"}, entry):
                        # 901 seconds: even a persisted fresh/stale flag cannot admit old numbers.
                        now = 10901 if unusable is entry else 10200
                        self.assertFalse(any(self.vm(unusable, now).usage[0]["windows"]))
                        self.assertNotIn("%", "".join(t for line in render._usage_header_rows(
                            [], now=now, usage_snapshots={"claude": unusable}) for t, _k in line))
                        with patch("time.time", return_value=now):
                            self.assertNotIn("%", usage_text({"usage_snapshots": {"claude": unusable}}))

    def test_admitted_stale_and_young_retry_keep_numbers_and_header_geometry(self):
        from tools.fleet.explorer.grid import cell_width
        from tools.fleet.explorer.state import reduce
        young = {**self.STALE, "freshness": "fresh", "observed_at": 10140}
        for entry in (self.STALE, young):
            vm = self.vm(entry, 10200)
            for width in (40, 80, 140, 180):
                for expanded in (False, True):
                    with self.subTest(entry=entry, width=width, expanded=expanded):
                        state = NavState(usage_open=expanded)
                        frame = draw(vm, state, width, 45, beat=1)
                        baseline = draw(self.vm(self.SUCCESS), state, width, 45, beat=1)
                        self.assertEqual(frame.hits, baseline.hits)
                        self.assertEqual(cell_width(frame.text_rows()[1]), width)
                        self.assertIn("less" if expanded else "more", frame.text_rows()[1])
                        if width >= 140:
                            text = frame.text_rows()[1]
                            self.assertIn("stale" if entry is self.STALE else "fresh", text)
                            self.assertIn("3m ago" if entry is self.STALE else "1m ago", text)
                            self.assertIn("API wait", text)
                            self.assertIn("rate limit", text)
                            self.assertIn("41%", text)
                            if expanded:
                                self.assertIn("27%", text)
                                self.assertEqual(text.index("5h"), baseline.text_rows()[1].index("5h"))
                                self.assertEqual(text.index("week"), baseline.text_rows()[1].index("week"))
                            if entry is self.STALE:
                                self.assertIn("·", text)
                        hit = next(hit for hit in frame.hits if hit.action == ("usage",))
                        changed, effects = reduce(state, {"type": "click", "action": hit.action}, frame)
                        self.assertEqual(changed.usage_open, not expanded)
                        self.assertFalse(effects)
            with patch("time.time", return_value=10200):
                summary = usage_text({"usage_snapshots": {"claude": entry}})
            self.assertIn("27%", summary)
            self.assertIn("3m ago" if entry is self.STALE else "1m ago", summary)
            self.assertIn("API wait", summary)

    def test_fixed_statuses_and_known_zero_at_narrow_folded_and_expanded_widths(self):
        from tools.fleet.explorer.grid import cell_width
        zero = {**self.SUCCESS, "payload": {"rl_5h": 0, "rl_7d": 0, "rl_ms": [["opus", 0]]}}
        for entry, now in ((self.SUCCESS, 10000), (self.STALE, 10200), (self.EXPIRED, 12000), (self.CHANGED, 12000), (zero, 10000)):
            vm = self.vm(entry, now)
            for width in (40, 60, 80, 100, 140, 180, 240, 320):
                for expanded in (False, True):
                    with self.subTest(entry=entry, width=width, expanded=expanded):
                        frame = draw(vm, NavState(usage_open=expanded), width, 45, beat=1)
                        self.assertEqual(cell_width(frame.text_rows()[1]), width)
                        self.assertIn("less" if expanded else "more", frame.text_rows()[1])
                        if entry is self.STALE:
                            if width >= 140:
                                self.assertIn("stale", frame.text_rows()[1])
                                self.assertIn("3m ago", frame.text_rows()[1])
                                self.assertIn("API wait", frame.text_rows()[1])
                                self.assertIn("41%", frame.text_rows()[1])
                                if expanded:
                                    self.assertIn("27%", frame.text_rows()[1])
                        if entry in (self.EXPIRED, self.CHANGED):
                            self.assertNotIn("%", frame.text_rows()[1])
                        if entry is zero and width >= 100:
                            self.assertIn("0%", frame.text_rows()[1])

    def test_legacy_header_and_system_summary_use_the_admitted_snapshot_only(self):
        from tools.fleet import render
        from tools.fleet.explorer.system import usage_text
        for entry, now, known in ((self.SUCCESS, 10000, True), (self.STALE, 10200, True),
                                  (self.EXPIRED, 12000, False), (self.CHANGED, 12000, False)):
            rows = render._usage_header_rows([], now=now, usage_snapshots={"claude": entry})
            text = "".join(text for row in rows for text, _key in row)
            with patch("time.time", return_value=now):
                summary = usage_text({"usage_snapshots": {"claude": entry}})
            self.assertEqual("%" in text, known)
            self.assertEqual("%" in summary, known)
            if known:
                self.assertIn("27%", text)
                self.assertIn("41%", text)
            if entry is self.STALE:
                for value in (text, summary):
                    self.assertIn("stale", value)
                    self.assertIn("3m ago", value)
                    self.assertIn("API wait", value)

    def test_precomputed_replay_and_pending_draw_do_not_resurrect_old_claude_numbers(self):
        from tools.fleet import render
        from tools.fleet.explorer.live import build_view_model
        from tools.fleet.explorer.view_model import persistable_view_model, restore_persisted_view_model
        from tools.fleet.tests.test_explorer_initial import _Screen
        old_rows = [{"harness": "claude", "text": "claude", "key": "hb_claude", "windows": [["week", 98, None]]},
                    {"text": "claude", "key": "hb_claude", "windows": [["week", 97, None]]},
                    {"harness": "codex", "text": "codex", "windows": [["week", 33, None]]}]
        base = ExplorerViewModel(clock=9000, usage=old_rows,
            readiness={"ready": False, "sources": {"basic": {"state": "unknown"}}},
            sessions=({"tag": "c7", "harness": "codex", "title": "old body"},))
        restored = restore_persisted_view_model(persistable_view_model(base))
        import tempfile
        from tools.fleet.explorer.runtime import ExplorerRuntime
        from tools.fleet.tests.test_fleet2_persisted_replay import complete_readiness
        with tempfile.TemporaryDirectory() as scratch:
            runtime = ExplorerRuntime(Path(scratch))
            runtime.configure_persistence({"claude", "codex"})
            self.assertTrue(runtime._write_cache(runtime._cache_envelope(base, complete_readiness())))
            replay = runtime.load_last_complete({"claude", "codex"})
            self.assertIsNotNone(replay)
        for vm in (restored, replay, build_view_model({"schema": 1, "sessions": [], "usage": old_rows}, now=12000),
                   build_view_model({"values": base.to_mapping()}, now=12000)):
            self.assertNotIn("98", repr(vm.usage))
            self.assertNotIn("97", repr(vm.usage))
            self.assertIn("33", repr(vm.usage))
        for entry in (self.STALE, {**self.STALE, "freshness": "fresh", "observed_at": 10140}):
            for fields in ({"schema": 1, "sessions": [], "usage": old_rows}, {"values": base.to_mapping()}):
                vm = build_view_model({**fields, "usage_snapshots": {"claude": entry}}, now=10200)
                self.assertEqual(vm.usage[0]["windows"][1][1], 41)
                self.assertNotIn("98", repr(vm.usage))
                self.assertNotIn("97", repr(vm.usage))
                self.assertIn("API wait", vm.usage[0]["text"])
        for loading in (True, False):
            young = {**self.STALE, "freshness": "fresh", "observed_at": 10140}
            for entry in (self.CHANGED, self.SUCCESS, self.STALE, young):
                with self.subTest(loading=loading, entry=entry), contextlib.ExitStack() as stack:
                    for name, value in {"_EXPLORER_ENABLED": True, "_EXPLORER_STATE": NavState(),
                                        "_EXPLORER_PUBLISHED_MODEL": base, "_EXPLORER_MODEL": base,
                                        "_EXPLORER_RESULT": None, "_EXPLORER_REPLAY_ACTIVE": True,
                                        "_EXPLORER_SHOWN": False, "_EXPLORER_READINESS": {"ready": False}}.items():
                        stack.enter_context(patch.object(render, name, value))
                    stack.enter_context(patch.object(render, "_explorer_runtime", return_value=SimpleNamespace(publication_ready=lambda _r: False)))
                    stack.enter_context(patch.object(render, "_explorer_compose", return_value=None))
                    now = 12000 if entry is self.CHANGED else 10000 if entry is self.SUCCESS else 10200
                    stack.enter_context(patch.object(render.time, "time", return_value=now))
                    painted = stack.enter_context(patch.object(render, "_draw_explorer"))
                    render._draw(_Screen(), [], [], "all", 0, loading=loading,
                                 snapshot={"usage_snapshots": {"claude": entry}})
                    text = painted.call_args.kwargs["frame"].text_rows()[1]
                    self.assertNotIn("98%", text)
                    self.assertNotIn("97%", text)
                    self.assertIn("unavailable" if entry is self.CHANGED else "41%", text)
                    if entry is self.STALE or entry is young:
                        self.assertIn("stale" if entry is self.STALE else "fresh", text)
                        self.assertIn("API wait", text)
                    if entry is not self.CHANGED:
                        # Current usage may advance while the body stays the exact
                        # previous complete base, without completing publication.
                        self.assertEqual(render._EXPLORER_MODEL.usage[0]["windows"][1][1], 41)
                    self.assertIs(render._EXPLORER_PUBLISHED_MODEL, base)
                    self.assertEqual(render._EXPLORER_MODEL.sessions, base.sessions)
                    self.assertEqual(render._EXPLORER_MODEL.readiness, base.readiness)

    def test_legacy_cached_terminal_row_is_rebound_without_rebuilding_the_body(self):
        from tools.fleet import render
        cached = [[("  usage ", "head"), ("claude", "hb_claude"), (" 98%", "lvl_r_flat")],
                  [("codex 33%", "hb_codex")], [("old body", "dim")]]
        screen = SimpleNamespace(getmaxyx=lambda: (45, 140), erase=lambda: None, noutrefresh=lambda: None)
        for entry, now, text in ((self.CHANGED, 12000, "unavailable"), (self.SUCCESS, 10000, "41%"),
                                 (self.STALE, 10200, "41%")):
            with self.subTest(entry=entry), contextlib.ExitStack() as stack:
                values = {"_EXPLORER_ENABLED": False, "_PROCESS_VIEW": False, "_SELECT_MODE": False,
                          "_PROMPT": None, "_OFFSET": 0, "_RELOAD_FRAME": cached,
                          "_RELOAD_FRAME_AT": 10, "_RELOAD_FRAME_COMPUTE_AT": None,
                          "_COMPUTE_HOSTS_SET_AT": None,
                          "_RELOAD_LAYOUT": {"schema_version": 1, "width": 140, "process_view": False,
                              "layout": render._layout_mode(140), "fixed_end": 2, "body_start": 2,
                              "body_end": 3, "legend_start": 3, "fixed_priorities": {0: 1, 1: 1}}}
                for name, value in values.items():
                    stack.enter_context(patch.object(render, name, value))
                stack.enter_context(patch.object(render.time, "monotonic", return_value=10))
                stack.enter_context(patch.object(render.time, "time", return_value=now))
                stack.enter_context(patch.object(render, "_build_lines", side_effect=AssertionError("body rebuild")))
                painted = stack.enter_context(patch.object(render, "_addline", return_value=[]))
                stack.enter_context(patch.object(render.curses, "doupdate"))
                render._draw(screen, [], [], "all", 0, loading=True, usage_snapshots={"claude": entry})
                rows = [call.args[2] for call in painted.call_args_list]
                self.assertIn(text, "".join(t for t, _k in rows[0]))
                if entry is self.STALE:
                    self.assertIn("stale", "".join(t for t, _k in rows[0]))
                    self.assertIn("API wait", "".join(t for t, _k in rows[0]))
                self.assertNotIn("98%", "".join(t for t, _k in rows[0]))
                self.assertEqual(rows[1:3], cached[1:3])
                self.assertEqual(cached[0][-1][0], " 98%")
                self.assertFalse(render._HIT_VALID)


class HeaderCallerFallback(unittest.TestCase):
    @staticmethod
    def snapshot():
        from tools.fleet.model import Session
        from tools.fleet.refresh import LiveSnapshot

        states = ["working"] * 15 + ["idle"] * 2 + ["blocked"]
        sessions = [Session(harness="codex", pid=index + 1, session_tag=f"h{index}",
                             cwd="/synthetic/repo", liveness=state)
                    for index, state in enumerate(states)]
        snapshot = LiveSnapshot(sessions=sessions, hearting={
            "version": "v3.50.3-20-gad247e2b6", "install_method": "unmanaged"})
        snapshot.usage_snapshots = {"codex": {"payload": {"rl_5h": 20, "rl_7d": 30},
                                               "observed_at": 2_000_000_000}}
        names = ("trainer-west-gpu-node-01", "cnn", "xavier", "larynx", "mpwav")
        snapshot.compute_hosts = {"hosts": [
            {"host": name, "self": index == 0, "reachable": True, "cpu_count": 4,
             "cpu_utilization_pct": 25, "memory_total_mib": 8192,
             "memory_used_mib": 4096, "gpus": []}
            for index, name in enumerate(names)
        ]}
        return snapshot, names[0]

    @staticmethod
    def runtime():
        return SimpleNamespace(prepare=lambda snapshot, detail, *, vm, generation, now: (vm, {}))

    def test_live_compose_keeps_source_host_and_draws_bounded_valid_header(self):
        from tools.fleet import render
        from tools.fleet.explorer.grid import cell_width

        snapshot, host_name = self.snapshot()
        with patch.object(render, "_explorer_runtime", return_value=self.runtime()):
            frame = render._explorer_compose(snapshot, 80, 45,
                                              NavState(view="OVERVIEW", scope="-"), now=2_000_000_000)
        row = frame.text_rows()[0]

        self.assertEqual(render._EXPLORER_MODEL.hosts[0]["name"], host_name)
        self.assertEqual(render._EXPLORER_MODEL.compute[0][0], host_name)
        self.assertTrue(row.startswith("⌂ trainer"), row)
        self.assertIn("…", row)
        self.assertIn("⠏ 15", row)
        self.assertIn("● 2", row)
        self.assertIn("● 1", row)
        self.assertIn("hearting v3.50.3-20-gad247e2b6 · unmanaged", row)
        self.assertRegex(row.rstrip(), r"\d{2}:\d{2}$")
        self.assertEqual(cell_width(row), 80)

    def test_render_once_caller_handles_a_valid_overbudget_compute_host(self):
        from tools.fleet import render
        from tools.fleet.explorer.grid import cell_width

        snapshot, host_name = self.snapshot()
        previous_mode = render._EXPLORER_ENABLED
        render.set_explorer_mode(True)
        try:
            with patch.object(render, "_explorer_runtime", return_value=self.runtime()), \
                    patch.object(render, "_collect_memory", return_value=None), \
                    patch.object(render, "_collect_governor", return_value=None), \
                    patch.object(render.gitinfo, "enrich_entities"), \
                    patch.dict(os.environ, {"FLEET_DEMO": "1", "COLUMNS": "80", "LINES": "45"}):
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    result = render.render_once(lambda harness_filter=None: snapshot, None, None)
        finally:
            render.set_explorer_mode(previous_mode)
        lines = output.getvalue().splitlines()
        self.assertEqual(result, 0)
        self.assertEqual(len(lines), 45)
        self.assertEqual(cell_width(lines[0]), 80)
        self.assertTrue(lines[0].startswith("⌂ trainer"), lines[0])
        self.assertIn("…", lines[0])
        self.assertIn("hearting v3.50.3-20-gad247e2b6 · unmanaged", lines[0])
        self.assertRegex(lines[0].rstrip(), r"\d{2}:\d{2}$")
        self.assertEqual(snapshot.compute_hosts["hosts"][0]["host"], host_name)


if __name__ == "__main__":
    unittest.main()
