"""Narrow typed replay and parity-helper contract checks."""
import unittest
from pathlib import Path
from types import SimpleNamespace

from tools.fleet.explorer.view_model import load_fixture
from tools.fleet.tests.parity_compare import diff_frames, diff_view_models, validate_ledger
from tools.fleet.tests.parity_states import enumerate_states
from tools.fleet.tests.reference_injection import inject_model_catalog
from tools.fleet.tests import snapshot_fixture
from tools.fleet.tests.snapshot_fixture import replay_snapshot, replay_source_bundle


FIXTURES = Path(__file__).parent / "fixtures" / "explorer"


class ReplayContract(unittest.TestCase):
    def test_source_bundle_join_rebuilds_typed_rows_and_pointer_provenance(self):
        state = {"sessions": [{"harness": "claude", "session_id": "sid-a", "session_tag": "aa",
                               "cwd": "/repo", "pid": 1}],
                 "jobs": [], "resource_jobs": [{"run_id": "resource-a", "cwd": "/repo"}],
                 "route": [{"route_id": "route-a", "nodes": []}],
                 "compute_hosts": {"hosts": []}, "peer_messages": {"records": []}}
        history = {"aa": {"seat": "seat-data", "cards": [], "handovers": []},
                   "_recent_handovers": [{"ts": "2026-10-20T01:00:00Z"}]}
        logs = {"sid-a:0": {"h": "claude", "sid": "sid-a", "lines": ["log row"]}}
        raw = {"lines": ["raw row"]}
        snapshot, detail = replay_source_bundle(state, history, logs, raw, now=10)
        ResourceJob, Session = snapshot_fixture._symbols("model", "ResourceJob", "Session")
        self.assertIsInstance(snapshot.sessions[0], Session)
        self.assertIsInstance(snapshot.resources[0], ResourceJob)
        self.assertEqual(snapshot.compute_hosts, {"hosts": []})
        self.assertEqual(detail.seats["claude:sid-a"][-1]["sid"], "sid-a")
        self.assertEqual(detail.source_records["aa.cards"], "/history_data.json/aa/cards")
        self.assertEqual(detail.source_records["logs.sid-a:0"], "/logs_hist.json/sid-a:0/lines")
        self.assertEqual(snapshot.observations["replay_sources"]["history_handovers"], history["_recent_handovers"])

    def test_snapshot_replay_preserves_allowlisted_values_and_unknown_provenance(self):
        snapshot, detail = replay_snapshot({"malformed": 2, "observations": {"ready": True},
                                            "extra_capture": {"sha256": "abc"}}, now=123)
        self.assertEqual(snapshot.malformed, 2)
        self.assertEqual(snapshot.observations["ready"], True)
        self.assertEqual(snapshot.observations["replay_unknown"]["extra_capture"]["sha256"], "abc")
        self.assertEqual(snapshot.observations["replay_now"], 123)
        self.assertIsNone(detail.focus)

    def test_snapshot_replay_rebuilds_nested_projection_dataclasses(self):
        ActiveNodeProjection, ProgressProjection, Session, WorkProjection = snapshot_fixture._symbols(
            "model", "ActiveNodeProjection", "ProgressProjection", "Session", "WorkProjection")
        snapshot, _detail = replay_snapshot({"sessions": [{"harness": "claude", "pid": 7,
            "work_projection": {"source": "route", "active_nodes": [{"id": "execute"}],
                                "progress": {"done": 2, "total": 4}, "unknown": "discarded"}}]})
        session = snapshot.sessions[0]
        self.assertIsInstance(session, Session)
        self.assertIsInstance(session.work_projection, WorkProjection)
        self.assertIsInstance(session.work_projection.active_nodes[0], ActiveNodeProjection)
        self.assertIsInstance(session.work_projection.progress, ProgressProjection)
        self.assertEqual(session.work_projection.progress.done, 2)
        self.assertFalse(hasattr(session.work_projection, "unknown"))

    def test_scrubbed_vm_fixture_loads_and_states_are_stable(self):
        vm = load_fixture(FIXTURES / "view_model.json")
        states = enumerate_states({"states": ("OVERVIEW",)}, vm)
        ids = [state["id"] for state in states]
        self.assertEqual(ids, sorted(set(ids)))
        self.assertTrue({"OVERVIEW", "PEERS", "ROUTES"}.issubset(ids))

    def test_differs_keep_list_order_and_ledger_summary_is_recomputed(self):
        self.assertEqual(diff_view_models({"ids": ["a", "b"]}, {"ids": ["b", "a"]})[0]["path"],
                         "/ids/0")
        ledger = {"differences": [{"id": "x", "axis": "a", "entity": "r", "field": "t",
                                    "states": ["OVERVIEW"], "widths": [140], "source": [],
                                    "reason": "observed", "classification": None}]}
        self.assertEqual(validate_ledger(ledger), [])
        self.assertEqual(ledger["summary"]["unclassified"], 1)

    def test_frame_diff_checks_cells_and_ordered_hits(self):
        left = {"cells": [["a", "b"]], "hits": [[0, 0, 1, ["view", "A"]]]}
        right = {"cells": [["a", "c"]], "hits": [[0, 0, 1, ["view", "B"]]]}
        diffs = diff_frames(left, right)
        self.assertEqual([item["kind"] for item in diffs], ["cell", "hits"])

    def test_model_catalog_injection_uses_provider_entries_and_main_only_config(self):
        renderer = SimpleNamespace()
        result = inject_model_catalog(renderer, {
            "providers": {"opencode": [{"id": "id-from-provider", "version": "visible version"}]},
            "main_only": ["configured-main-only"],
        })
        self.assertEqual(renderer.MODEL_CHOICES, {"opencode": ["id-from-provider"]})
        self.assertEqual(renderer.MODEL_SHOWN, {"id-from-provider": "visible version"})
        self.assertEqual(renderer.MAIN_ONLY, ("configured-main-only",))
        self.assertEqual(result["model_counts"], {"opencode": 1})


class ReplayClassIdentity(unittest.TestCase):
    def test_rows_are_built_from_the_adapters_classes_whatever_the_import_path(self):
        import importlib
        import sys
        root = Path(__file__).resolve().parents[2]
        sys.path.insert(0, str(root))                 # `fleet` as a second top-level name for the same package
        try:
            other = importlib.import_module("fleet.model")
        finally:
            sys.path.remove(str(root))
        (Session,) = snapshot_fixture._symbols("model", "Session")
        self.assertIsNot(other.Session, Session)
        snapshot, _detail = replay_snapshot({"sessions": [{"harness": "claude", "pid": 1}]})
        self.assertIsInstance(snapshot.sessions[0], Session)
        self.assertNotIsInstance(snapshot.sessions[0], other.Session)


class KeyedLedger(unittest.TestCase):
    def test_a_pure_reorder_is_one_entry_and_no_field_cascade(self):
        from tools.fleet.tests.parity_compare import diff_view_models_keyed
        left = {"sessions": [{"tag": "a", "age": "1m"}, {"tag": "b", "age": "2m"}]}
        right = {"sessions": [{"tag": "b", "age": "2m"}, {"tag": "a", "age": "1m"}]}
        differences, _ = diff_view_models_keyed(left, right)
        self.assertEqual([path for path, _l, _r in differences], ["/sessions/·order"])

    def test_numbers_and_unknown_spellings_compare_equal(self):
        from tools.fleet.tests.parity_compare import diff_view_models_keyed
        left = {"sessions": [{"tag": "a", "n": 8, "pane": "·", "now": ""}]}
        right = {"sessions": [{"tag": "a", "n": 8.0, "pane": None, "now": None}]}
        self.assertEqual(diff_view_models_keyed(left, right)[0], [])

    def test_route_aliases_resolve_through_the_stage_crosswalk(self):
        from tools.fleet.tests.parity_compare import diff_view_models_keyed
        stages = [{"name": "plan"}, {"name": "execute"}]
        left = {"routes": [{"id": "row1", "session_tag": "09", "stages": stages, "age": "1m"}]}
        right = {"routes": [{"id": "rt-1", "session_tag": "09", "stages": stages, "age": "2m"}]}
        differences, crosswalk = diff_view_models_keyed(left, right)
        self.assertEqual(crosswalk["row1"]["route_id"], "rt-1")
        self.assertEqual([path for path, _l, _r in differences], ["/routes/row1/id", "/routes/row1/age"])

    def test_reference_route_words_equal_to_the_derived_words_are_not_a_difference(self):
        from tools.fleet.tests.parity_compare import diff_view_models_keyed
        route = {"id": "r1", "session_tag": "09", "title": "t", "age": "42m", "shape": "staged",
                 "legs": [["frame", "done", None], ["code", "active", None]]}
        words = {"detail_title": "t", "detail_trailer": "42m · 2 legs", "detail_after": "before this leg: frame ✓ — outside this snapshot"}
        left = {"routes": [dict(route, **words)], "nodes": {"r1": [{"kind": "leg"}]}}
        right = {"routes": [dict(route)], "nodes": {"r1": [{"kind": "leg"}]}}
        self.assertEqual(diff_view_models_keyed(left, right)[0], [])
        # words that say more than the record derives stay a difference
        left["routes"][0]["detail_title"] = "a title the record does not carry"
        paths = [path for path, _l, _r in diff_view_models_keyed(left, right)[0]]
        self.assertEqual(paths, ["/routes/r1/detail_title"])

    def test_class_two_is_refused_when_the_reference_text_has_a_source(self):
        from tools.fleet.tests.parity_compare import build_live_ledger
        rules = [{"id": "no-source", "match": "^/sessions/a/title$", "classification": 2, "reason": "none"}]
        expected = {"sessions": [{"tag": "a", "title": "a distinctive reference title"}]}
        actual = {"sessions": [{"tag": "a", "title": "other"}]}
        sourced = build_live_ledger(expected, actual, capture={}, rules=rules,
                                    texts={"state.json": "x a distinctive reference title y"})
        self.assertEqual((sourced["differences"][0]["classification"], sourced["summary"]["class1_residue"]), (1, 1))
        unsourced = build_live_ledger(expected, actual, capture={}, rules=rules, texts={"state.json": "nothing"})
        self.assertEqual(unsourced["differences"][0]["classification"], 2)
        self.assertEqual(unsourced["differences"][0]["production_value"], "·")
        self.assertEqual(unsourced["summary"]["class1_residue"], 0)

    def test_overlay_keeps_live_values_only_where_the_sides_are_equivalent(self):
        from tools.fleet.tests.parity_compare import overlay_view_model
        merged = overlay_view_model({"a": 8, "b": "hand", "c": ""}, {"a": 8.0, "b": "live", "c": None})
        self.assertEqual((merged["a"], merged["b"], merged["c"]), (8.0, "hand", None))


if __name__ == "__main__":
    unittest.main()
