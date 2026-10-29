"""A live snapshot becomes view-model records of exactly the fixture's shape, section by section.

The screens read the fixture's records positionally and by key. A live builder that renames a field,
puts an object where the fixture has a pair, or changes a type breaks a screen only on real data
(that is how a ``ResourceJob`` object once reached the COMPUTE rail). Each section is compared here:
the live record's keys are drawn from the fixture's keys, its value types are the fixture's types,
and an unknown value is ``None``. Additive route fields have the explicit
optional contract below, so old render fixtures remain valid inputs.
"""
import time
import unittest
from pathlib import Path

from tools.fleet.explorer.live import build_view_model
from tools.fleet.explorer.view_model import load_fixture
from tools.fleet.model import Session
from tools.fleet.tests import explorer_fixture as ef

FIXTURE = Path(__file__).parent / "fixtures" / "explorer" / "view_model.json"
HARNESSES = ("claude", "codex", "opencode")
FREE_KEYED = frozenset(("peer_edges.cards", "dialogue.logs", "models.shown", "models.profile_map", "models.model_choices",
                        "models.main_only", "models.pools", "models.context", "models.context_source",
                        "models.token_units", "models.effort_keys"))
# Sections only the selection pump's reads can fill: without them they are omitted (empty), never invented.
OMITTED = ("seats", "generations", "cards", "dialogue", "attempts", "models")


def route_extension(key, value):
    """Validate only the three public route additions, including known zero."""
    optional_number = lambda item: item is None or type(item) in (int, float)
    optional_pct = lambda item: item is None or type(item) is int
    pair = lambda item: isinstance(item, (list, tuple)) and len(item) == 2
    if key == "owner_gauge":
        return pair(value) and optional_pct(value[0]) and optional_number(value[1])
    if key == "current":
        return type(value) is bool
    if key == "worker_contexts":
        return isinstance(value, (list, tuple)) and all(
            isinstance(row, dict) and set(row) == {"stage", "harness", "model", "context", "window"}
            and isinstance(row["stage"], str)
            and (row["harness"] is None or isinstance(row["harness"], str))
            and (row["model"] is None or pair(row["model"]) and all(
                isinstance(item, str) for item in row["model"]))
            and optional_pct(row["context"]) and optional_number(row["window"])
            for row in value)
    raise ValueError(key)


def bucket(value):
    if value is None:
        return "none"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "str"
    if isinstance(value, dict) or hasattr(value, "keys"):
        return "map"
    if isinstance(value, (list, tuple)):
        return "list"
    return type(value).__name__


def conform(live, samples, path, problems):
    """Record in ``problems`` how ``live`` differs in shape from the fixture ``samples``."""
    kind = bucket(live)
    if kind == "none":
        return                                  # unknown is always allowed (drawn ·)
    sample_kinds = {bucket(sample) for sample in samples} - {"none"}
    if not sample_kinds:
        return
    if kind not in sample_kinds:
        problems.append(f"{path}: live {kind}, fixture {sorted(sample_kinds)}")
        return
    if kind == "map" and path in FREE_KEYED:
        # Keys are data (a model name, a "tag:index" log): every live value has the shape of some fixture value.
        values = [item for sample in samples if bucket(sample) == "map" for item in sample.values()]
        for key, value in live.items():
            conform(value, values, f"{path}[{key}]", problems)
        return
    if kind == "map":
        keys = set().union(*(set(sample) for sample in samples if bucket(sample) == "map"))
        for key in live:
            if path == "routes[]" and key in ("owner_gauge", "worker_contexts", "current"):
                if not route_extension(key, live[key]):
                    problems.append(f"{path}.{key}: invalid optional route field shape")
                continue
            if path == "system.gpu_processes[]" and key == "session_tag":
                if live[key] is not None and not isinstance(live[key], str):
                    problems.append(f"{path}.session_tag: expected optional string")
                continue
            if path == "system" and key in ("gpu_process_summary", "unregistered_gpu_summary"):
                if not isinstance(live[key], dict) or set(live[key]) - {"count", "unregistered", "owner", "remaining"}:
                    problems.append(f"system.{key}: invalid summary shape")
                continue
            if key not in keys:
                if path == "peer_edges" and key == "cards":          # PEERS field contract: owner-stage rows per card
                    if not all(isinstance(card, dict) and all(
                            isinstance(owner, dict) and set(owner) == {"state", "label", "stages"} and all(
                                set(stage) == {"name", "state"} for stage in owner["stages"])
                            for owner in card.get("owners", ())) for card in live[key].values()):
                        problems.append("peer_edges.cards: invalid owner shape")
                    continue
                if path == "peer_edges" and key == "watches":        # PEERS field contract: steward watch footers
                    if not all(isinstance(item, dict) and set(item) == {"repo", "text"} for item in live[key]):
                        problems.append("peer_edges.watches: invalid shape")
                    continue
                if path == "system" and key == "gpu_processes":
                    for index, process in enumerate(live[key]):
                        if not isinstance(process, dict) or set(process) - {
                                "host", "gpu", "session_id", "session_tag", "cwd", "command", "vram", "age"}:
                            problems.append(f"system.gpu_processes[{index}]: invalid process record shape")
                    continue
                if path == "system" and key == "unregistered_gpu_processes":
                    if any(not isinstance(row, (list, tuple)) or len(row) != 5 for row in live[key]):
                        problems.append("system.unregistered_gpu_processes: invalid positional shape")
                    continue
                problems.append(f"{path}.{key}: field the fixture does not have")
                continue
            conform(live[key], [s[key] for s in samples if bucket(s) == "map" and key in s],
                    f"{path}.{key}", problems)
    elif kind == "list":
        lists = [sample for sample in samples if bucket(sample) == "list" and sample]
        lengths = {len(item) for item in lists}
        width = next(iter(lengths)) if len(lengths) == 1 else 0
        kinds = [{bucket(item[i]) for item in lists} - {"none"} for i in range(width)]
        positional = width > 1 and any(kinds[i] != kinds[0] for i in range(1, width))
        if positional and len(live) == width:
            for index, value in enumerate(live):
                conform(value, [item[index] for item in lists], f"{path}[{index}]", problems)
        else:
            flat = [element for item in lists for element in item]
            for index, value in enumerate(live):
                conform(value, flat, f"{path}[]", problems)


def rich_model(with_detail=True):
    """The demo snapshot with the pump's results, so every producible section exists."""
    return ef.demo_model(detail=with_detail)


class SectionShapes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = load_fixture(FIXTURE).to_mapping()
        cls.live = rich_model().to_mapping()
        cls.bare = rich_model(with_detail=False).to_mapping()

    def check(self, section):
        problems = []
        live, fixture = self.live[section], self.fixture[section]
        if isinstance(fixture, dict) and isinstance(live, dict) and section in ("nodes", "generations", "cards"):
            live_items, fixture_items = list(live.values()), [v for v in fixture.values()]
            for item in live_items:
                conform(item, fixture_items, section, problems)
        else:
            conform(live, [fixture], section, problems)
        self.assertEqual(problems, [], section)

    def test_every_section_the_builder_fills_has_the_fixture_shape(self):
        for section in ("hosts", "usage", "repos", "sessions", "routes", "nodes", "questions",
                        "seats", "generations", "cards", "dialogue", "attempts", "models",
                        "peer_nodes", "peer_edges", "compute", "memory", "events", "system"):
            with self.subTest(section=section):
                self.assertTrue(self.live[section], f"live {section} is empty — the sample has no source")
                self.check(section)

    def test_answered_records_are_the_recorded_user_answers_and_have_the_fixture_shape(self):
        from tools.fleet.refresh import LiveSnapshot
        snapshot = LiveSnapshot(memory={"by_repo": {"demo-app": [
            {"ts": "2026-10-23T12:00:00", "action": "decision-record", "type": "decision",
             "snippet": "[결정] 이대로 시작할까요?"},
            {"ts": "2026-10-23T11:00:00", "action": "add", "type": "decision", "snippet": "not an answer"}]}})
        live = build_view_model(snapshot, now=time.time()).to_mapping()
        self.assertEqual(live["answered"], [["demo-app", [["12:00", "이대로 시작할까요?"]]]])
        problems = []
        conform(live["answered"], [self.fixture["answered"]], "answered", problems)
        self.assertEqual(problems, [])

    def test_sections_without_a_source_are_omitted_not_invented(self):
        for section in OMITTED:
            with self.subTest(section=section):
                self.assertFalse(self.bare[section])

    def test_session_records_carry_the_same_fields_for_every_harness(self):
        by_harness = {}
        for row in self.live["sessions"]:
            by_harness.setdefault(row["harness"], set()).add(frozenset(row))
        self.assertEqual(set(by_harness), set(HARNESSES))
        fields = {next(iter(sets)) for sets in by_harness.values()}
        self.assertEqual(len(fields), 1)
        self.assertEqual(set(next(iter(fields))), set(self.fixture["sessions"][0]))

    def test_compute_rows_are_positional_like_the_fixture(self):
        for host in self.live["compute"]:
            self.assertEqual(len(host), len(self.fixture["compute"][0]))
        reachable = [host for host in self.live["compute"] if host[5]]
        self.assertTrue(reachable)
        for gpu in reachable[0][5]:
            self.assertEqual(len(gpu), len(self.fixture["compute"][0][5][0]))

    def test_unreadable_host_and_unknown_values_stay_none(self):
        gone = next(host for host in self.live["compute"] if host[0] == "gone")
        self.assertEqual(gone[2:5], [None, None, None])
        self.assertEqual(gone[5], [])
        unknown = Session(harness="claude", pid=1, cwd="/synthetic/repo", session_id="s-unknown",
                          session_tag="ab")
        vm = build_view_model({"sessions": [unknown], "project_of": lambda _cwd: "synthetic-repo"})
        row = vm.sessions[0]
        for name in ("model", "effort", "title", "context", "window", "now", "ask", "pane", "branch",
                     "generation", "last_clear", "running", "gpu", "pane_meta"):
            self.assertIsNone(row[name], name)
        self.assertEqual(row["tag"], "ab")

    def test_a_zero_is_a_value_not_unknown(self):
        zero = Session(harness="codex", pid=2, cwd="/synthetic/repo", session_id="s-zero",
                       session_tag="cd", ctx_pct=0, elapsed_min=0)
        vm = build_view_model({"sessions": [zero], "project_of": lambda _cwd: "synthetic-repo"})
        self.assertEqual(vm.sessions[0]["context"], 0)
        self.assertEqual(vm.sessions[0]["age"], "0m")

    def test_route_extensions_accept_known_zero_unknown_and_legacy_records(self):
        samples = self.fixture["routes"]
        for fields in ({}, {"owner_gauge": [None, None], "worker_contexts": [], "current": False},
                       {"owner_gauge": (0, 1.0), "current": True, "worker_contexts": [
                           {"stage": "execute", "harness": "codex", "model": ["model", "fam_other"],
                            "context": 0, "window": 1.0},
                           {"stage": "test", "harness": None, "model": None,
                            "context": None, "window": None}]}):
            with self.subTest(fields=fields):
                problems = []
                conform({**samples[0], **fields}, samples, "routes[]", problems)
                self.assertEqual(problems, [])

    def test_route_extensions_reject_wrong_types_and_unlisted_fields(self):
        worker = {"stage": "execute", "harness": "codex", "model": None, "context": 0, "window": 1}
        invalid = [
            {"owner_gauge": None}, {"worker_contexts": None}, {"current": None},
            {"owner_gauge": [0]}, {"owner_gauge": [0, 1, 2]}, {"owner_gauge": [False, 1]},
            {"owner_gauge": ["0", 1]}, {"owner_gauge": [0, "1M"]},
            {"current": 0}, {"current": "true"}, {"worker_contexts": {}},
            {"worker_contexts": [dict(worker, context=False)]},
            {"worker_contexts": [dict(worker, window="1M")]},
            {"worker_contexts": [dict(worker, harness=7)]},
            {"worker_contexts": [dict(worker, stage=None)]},
            {"worker_contexts": [dict(worker, model=["model"])]},
            {"worker_contexts": [dict(worker, model=["model", 7])]},
            {"worker_contexts": [dict(worker, foreign=1)]},
            {"worker_contexts": [{key: val for key, val in worker.items() if key != "context"}]},
            {"future_unknown_field": 0},
        ]
        for fields in invalid:
            with self.subTest(fields=fields):
                problems = []
                conform({**self.fixture["routes"][0], **fields}, self.fixture["routes"], "routes[]", problems)
                self.assertTrue(problems)


class CensusHeaderTests(unittest.TestCase):
    @staticmethod
    def _header_vm(*, session_states, hosts, version, freshness):
        from tools.fleet.explorer.view_model import ExplorerViewModel

        return ExplorerViewModel(
            sessions=[{"state": state} for state in session_states], hosts=hosts,
            hearting={"version": version, "install_method": "unmanaged"},
            system={"install": {"version": version}, "usage_api": {"freshness": freshness}})

    def test_exact_fit_census_header_keeps_saved_spacing_and_full_freshness(self):
        from tools.fleet.explorer.frame import draw
        from tools.fleet.explorer.grid import cell_width
        from tools.fleet.explorer.state import NavState
        from tools.fleet.explorer.view_model import ExplorerViewModel

        census = {"parts": [("⠏ 0", "spin"), (" working", "dim"), ("  ● 0", "idle"),
                             (" idle", "dim"), ("  ● 0", "blocked"), (" answer needed", "dim"),
                             ("   ", "branch_s")]}
        version, freshness = "v3.50.3-20-gad247e2b6", "02:38"
        vm = ExplorerViewModel(census=census, system={"install": {"version": version},
                                                      "usage_api": {"freshness": freshness}})
        expected = ("⠏ 0 working  ● 0 idle  ● 0 answer needed   hearting "
                    "v3.50.3-20-gad247e2b6 · unmanaged  02:38")
        width = cell_width(expected) + 1
        frame, trace = ef.trace_draw(draw, vm, NavState(view="OVERVIEW", scope="-"), width, 45)

        row = frame.text_rows()[0]

        self.assertEqual(row.rstrip(), expected)
        self.assertTrue(row.startswith("⠏ 0 working"), row)
        self.assertIn(version, row)
        self.assertTrue(row.rstrip().endswith(freshness), row)
        self.assertEqual(ef.frame_violations(frame, trace, width, 45), [])

    def test_exact_fit_header_keeps_host_identity_and_existing_census_spacing(self):
        from tools.fleet.explorer.frame import draw
        from tools.fleet.explorer.state import NavState
        from tools.fleet.explorer.view_model import ExplorerViewModel

        census = {"parts": [("⠏ 0", "spin"), (" working", "dim"), ("  ● 0", "idle"),
                             (" idle", "dim"), ("  ● 0", "blocked"), (" answer needed", "dim"),
                             ("   ", "branch_s")]}
        version, freshness = "v3.50.3-20-gad247e2b6", "02:38"
        vm = ExplorerViewModel(hosts=[{"mark": "⌂", "name": "moving4"}], census=census,
                               system={"install": {"version": version},
                                       "usage_api": {"freshness": freshness}})
        frame, trace = ef.trace_draw(draw, vm, NavState(view="OVERVIEW", scope="-"), 120, 45)
        row = frame.text_rows()[0]

        self.assertTrue(row.startswith(" hosts ⌂ moving4 1/1"), row)
        self.assertTrue(row.rstrip().endswith(
            "⠏ 0 working  ● 0 idle  ● 0 answer needed   hearting " + version +
            " · unmanaged  " + freshness), row)
        self.assertEqual(ef.frame_violations(frame, trace, 120, 45), [])

    def test_overlong_demo_census_keeps_counts_version_and_full_clock(self):
        from tools.fleet.explorer.frame import draw
        from tools.fleet.explorer.state import NavState

        vm = ef.demo_model()
        frame, trace = ef.trace_draw(draw, vm, NavState(view="OVERVIEW", scope="-"), 80, 45)
        row = frame.text_rows()[0]
        freshness = vm.system["usage_api"]["freshness"]
        version = vm.system["install"]["version"]

        self.assertTrue(row.startswith("⌂ base +2"), row)
        self.assertIn("⠏ 3 ● 5 ● 2", row)
        self.assertIn(version, row)
        self.assertIn("hearting " + version + " · unmanaged", row)
        self.assertTrue(row.rstrip().endswith(freshness), row)
        self.assertEqual(ef.frame_violations(frame, trace, 80, 45), [])

    def test_18_session_moving4_header_keeps_identity_counts_version_and_clock_in_80_cells(self):
        from tools.fleet.explorer.frame import draw
        from tools.fleet.explorer.grid import cell_width
        from tools.fleet.explorer.state import NavState

        hosts = [{"mark": "⌂", "name": "moving4"},
                 {"mark": "◇", "name": "cnn"}, {"mark": "◇", "name": "xavier"},
                 {"mark": "◇", "name": "larynx"}, {"mark": "◇", "name": "mpwav"}]
        states = ["working"] * 15 + ["idle"] * 2 + ["blocked"]
        version, freshness = "v3.50.3-20-gad247e2b6", "02:38"
        vm = self._header_vm(session_states=states, hosts=hosts, version=version, freshness=freshness)
        frame, trace = ef.trace_draw(draw, vm, NavState(view="OVERVIEW", scope="-"), 80, 45)
        row = frame.text_rows()[0]

        self.assertEqual(len(states), 18)
        self.assertTrue(row.startswith("⌂ moving4 +4"), row)
        for fragment in ("⠏ 15", "● 2", "● 1", version, "hearting " + version + " · unmanaged", freshness):
            self.assertIn(fragment, row)
        self.assertTrue(row.rstrip().endswith(freshness), row)
        self.assertEqual(cell_width(row), 80)
        self.assertEqual(ef.frame_violations(frame, trace, 80, 45), [])
        self.assertFalse([item for item in trace if item[0] == "clip" and item[2] == 0])

    def test_meaningful_repeated_spaces_survive_compaction(self):
        from tools.fleet.explorer.frame import draw
        from tools.fleet.explorer.state import NavState

        host_name, version, freshness = "moving  4", "v3  build", "02:38  fresh"
        vm = self._header_vm(session_states=["working"] * 15 + ["idle"] * 2 + ["blocked"],
                             hosts=[{"mark": "⌂", "name": host_name}],
                             version=version, freshness=freshness)
        frame, trace = ef.trace_draw(draw, vm, NavState(view="OVERVIEW", scope="-"), 80, 45)
        row = frame.text_rows()[0]

        self.assertIn("⌂ " + host_name, row)
        self.assertIn(version, row)
        self.assertTrue(row.rstrip().endswith(freshness), row)
        self.assertEqual(ef.frame_violations(frame, trace, 80, 45), [])

    def test_overlong_version_is_visibly_bounded_and_does_not_escape_drawing(self):
        from tools.fleet.explorer.state import NavState
        from tools.fleet.explorer.frame import draw
        from tools.fleet.explorer.grid import cell_width

        version = "v" + "x" * 100
        vm = self._header_vm(session_states=["working"] * 15 + ["idle"] * 2 + ["blocked"],
                             hosts=[{"mark": "⌂", "name": "moving4"}],
                             version=version, freshness="02:38")
        frame, trace = ef.trace_draw(draw, vm, NavState(view="OVERVIEW", scope="-"), 80, 45)
        row = frame.text_rows()[0]

        self.assertIn("⌂ moving4", row)
        self.assertIn("hearting", row)
        self.assertIn("unmanaged", row)
        self.assertIn("…", row)
        self.assertTrue(row.rstrip().endswith("02:38"), row)
        self.assertEqual(vm.hearting["version"], version)
        self.assertEqual(cell_width(row), 80)
        self.assertEqual(ef.frame_violations(frame, trace, 80, 45), [])
        self.assertFalse([item for item in trace if item[0] == "clip" and item[2] == 0])

    def test_valid_overbudget_host_gets_a_bounded_header_token(self):
        from tools.fleet.explorer.frame import draw
        from tools.fleet.explorer.grid import cell_width
        from tools.fleet.explorer.state import NavState

        host_name = "trainer-west-gpu-node-01"
        hosts = [{"mark": "⌂", "name": host_name}, {"mark": "◇", "name": "cnn"},
                 {"mark": "◇", "name": "xavier"}, {"mark": "◇", "name": "larynx"},
                 {"mark": "◇", "name": "mpwav"}]
        version, freshness = "v3.50.3-20-gad247e2b6", "02:38"
        vm = self._header_vm(session_states=["working"] * 15 + ["idle"] * 2 + ["blocked"],
                             hosts=hosts, version=version, freshness=freshness)
        frame, trace = ef.trace_draw(draw, vm, NavState(view="OVERVIEW", scope="-"), 80, 45)
        row = frame.text_rows()[0]

        self.assertTrue(row.startswith("⌂ trainer"), row)
        self.assertIn("…", row)
        self.assertIn("+4", row)
        self.assertIn("⠏ 15", row)
        self.assertIn("● 2", row)
        self.assertIn("● 1", row)
        self.assertIn("hearting " + version + " · unmanaged", row)
        self.assertTrue(row.rstrip().endswith(freshness), row)
        self.assertEqual(vm.hosts[0]["name"], host_name)
        self.assertEqual(cell_width(row), 80)
        self.assertEqual(ef.frame_violations(frame, trace, 80, 45), [])

    def test_nohost_overlong_census_and_version_still_draw_a_bounded_header(self):
        from tools.fleet.explorer.frame import draw
        from tools.fleet.explorer.grid import cell_width
        from tools.fleet.explorer.state import NavState
        from tools.fleet.explorer.view_model import ExplorerViewModel

        census_count = "12345678901234567890"
        census = {"parts": [("⠏ " + census_count, "spin"), (" working", "dim"),
                             ("  ● 234567890", "idle"), (" idle", "dim"),
                             ("  ● 345678901", "blocked"), (" answer needed", "dim")]}
        version, freshness = "v" + "x" * 100, "02:38"
        vm = ExplorerViewModel(census=census, hearting={"version": version,
                                                       "install_method": "unmanaged"},
                               system={"usage_api": {"freshness": freshness}})
        frame, trace = ef.trace_draw(draw, vm, NavState(view="OVERVIEW", scope="-"), 80, 45)
        row = frame.text_rows()[0]

        self.assertIn("⠏", row)
        self.assertIn("●", row)
        self.assertIn("hearting", row)
        self.assertIn("unmanaged", row)
        self.assertIn("…", row)
        self.assertTrue(row.rstrip().endswith(freshness), row)
        self.assertEqual(vm.census["parts"][0][0], "⠏ " + census_count)
        self.assertEqual(vm.hearting["version"], version)
        self.assertLessEqual(cell_width(row), 80)
        self.assertEqual(ef.frame_violations(frame, trace, 80, 45), [])

    def test_ultranarrow_rows_keep_visible_clips_for_identity_and_clock(self):
        from tools.fleet.explorer.frame import draw
        from tools.fleet.explorer.grid import cell_width
        from tools.fleet.explorer.state import NavState
        from tools.fleet.explorer.view_model import ExplorerViewModel

        host_name, version, freshness = "trainer-west-gpu-node-01", "v" + "x" * 100, "02:38"
        vm = self._header_vm(session_states=["working"] * 15 + ["idle"] * 2 + ["blocked"],
                             hosts=[{"mark": "⌂", "name": host_name}],
                             version=version, freshness=freshness)
        for width in (40, 32):
            with self.subTest(width=width):
                frame, trace = ef.trace_draw(draw, vm, NavState(view="OVERVIEW", scope="-"), width, 24)
                row = frame.text_rows()[0]
                self.assertIn("…", row)
                self.assertIn("⠏…", row)
                self.assertIn("●…", row)
                self.assertIn("hearting …", row)
                self.assertTrue(row.rstrip().endswith(freshness), row)
                self.assertEqual(cell_width(row), width)
                self.assertEqual(vm.hearting["version"], version)
                self.assertEqual(vm.hosts[0]["name"], host_name)
                self.assertFalse([item for item in trace if item[0] == "clip" and item[2] == 0])

    def test_missing_install_metadata_uses_existing_unknown_unmanaged_fallbacks(self):
        from tools.fleet.explorer.frame import draw
        from tools.fleet.explorer.state import NavState
        from tools.fleet.explorer.view_model import ExplorerViewModel

        vm = ExplorerViewModel(sessions=[{"state": "idle"}],
                               system={"usage_api": {"freshness": "02:38"}})
        frame, trace = ef.trace_draw(draw, vm, NavState(view="OVERVIEW", scope="-"), 120, 45)
        row = frame.text_rows()[0]

        self.assertIn("hearting unknown · unmanaged", row)
        self.assertTrue(row.rstrip().endswith("02:38"), row)
        self.assertEqual(ef.frame_violations(frame, trace, 120, 45), [])

    def test_overlong_multi_digit_counts_keep_all_census_information(self):
        from tools.fleet.explorer.frame import draw
        from tools.fleet.explorer.state import NavState
        from tools.fleet.explorer.view_model import ExplorerViewModel

        census = {"parts": [("⠏ 12", "spin"), (" working", "dim"), ("  ● 34", "idle"),
                             (" idle", "dim"), ("  ● 56", "blocked"), (" answer needed", "dim"),
                             ("   ", "branch_s")]}
        version, freshness = "v3.50.3-20-gad247e2b6", "02:38"
        vm = ExplorerViewModel(census=census, system={"install": {"version": version},
                                                      "usage_api": {"freshness": freshness}})
        frame, trace = ef.trace_draw(draw, vm, NavState(view="OVERVIEW", scope="-"), 80, 45)
        row = frame.text_rows()[0]

        self.assertIn("⠏ 12 ● 34 ● 56", row)
        self.assertIn(version, row)
        self.assertIn("hearting " + version + " · unmanaged", row)
        self.assertTrue(row.rstrip().endswith(freshness), row)
        self.assertEqual(ef.frame_violations(frame, trace, 80, 45), [])

    def test_wide_version_cell_at_the_right_boundary_keeps_the_full_clock(self):
        from tools.fleet.explorer.frame import draw
        from tools.fleet.explorer.grid import cell_width
        from tools.fleet.explorer.state import NavState
        from tools.fleet.explorer.view_model import ExplorerViewModel

        census = {"parts": [("⠏ 0", "spin"), (" working", "dim"), ("  ● 0", "idle"),
                             (" idle", "dim"), ("  ● 0", "blocked"), (" answer needed", "dim"),
                             ("   ", "branch_s")]}
        version, freshness = "v3.50.3-20-gad247e2界6", "02:38"
        vm = ExplorerViewModel(census=census, system={"install": {"version": version},
                                                      "usage_api": {"freshness": freshness}})
        frame, trace = ef.trace_draw(draw, vm, NavState(view="OVERVIEW", scope="-"), 80, 45)
        row = frame.text_rows()[0]

        self.assertIn(version, row)
        self.assertTrue(row.rstrip().endswith(freshness), row)
        self.assertEqual(cell_width(row), 80)
        self.assertEqual(ef.frame_violations(frame, trace, 80, 45), [])

    def test_adjacent_wide_host_cell_keeps_its_continuation_before_the_census(self):
        from tools.fleet.explorer.frame import draw
        from tools.fleet.explorer.state import NavState

        version, freshness = "v3.50.3-20-gad247e2界6", "02:38"
        hosts = [{"mark": "⌂", "name": "moving界4"}, {"mark": "◇", "name": "cnn"},
                 {"mark": "◇", "name": "xavier"}, {"mark": "◇", "name": "larynx"},
                 {"mark": "◇", "name": "mpwav"}]
        vm = self._header_vm(session_states=["working"] * 15 + ["idle"] * 2 + ["blocked"],
                             hosts=hosts, version=version, freshness=freshness)
        frame, trace = ef.trace_draw(draw, vm, NavState(view="OVERVIEW", scope="-"), 80, 45)
        row = frame.text_rows()[0]
        cells = frame.cells[0]
        wide_at = next(index for index, cell in enumerate(cells) if cell[0] == "界")

        self.assertTrue(row.startswith("⌂ moving界4 +4"), row)
        self.assertEqual(cells[wide_at + 1][0], "")
        self.assertEqual(cells[wide_at + 1][1:], cells[wide_at][1:])
        self.assertTrue(any(cell[0] == "⠏" for cell in cells[wide_at + 2:]))
        self.assertIn(version, row)
        self.assertTrue(row.rstrip().endswith(freshness), row)
        self.assertEqual(ef.frame_violations(frame, trace, 80, 45), [])


if __name__ == "__main__":
    unittest.main()
