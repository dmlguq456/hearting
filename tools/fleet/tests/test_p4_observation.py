"""Fleet preserves typed diagnostics and the origin of memory changes."""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../../run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import datetime
from contextlib import ExitStack, redirect_stdout
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from fleet import collectors, fleet, render
from fleet.collectors import dispatch, memory, resource_runs
from fleet.model import Session


def flatten(rows):
    return "\n".join("".join(text for text, _ in row) for row in rows if row)


class ObservationTest(unittest.TestCase):
    def tearDown(self):
        render.set_show_all(False)
        render.set_process_view(False)

    def test_resource_diagnostics_never_become_jobs_log_errors(self):
        diagnostic = {"kind": "missing-registry", "path": "/work/project/old.json",
                      "reason": "registered-path-absent", "blocking": False}
        with mock.patch.object(resource_runs, "_shared") as shared, \
             mock.patch.object(dispatch.collect, "last_malformed", 0):
            shared.return_value.scan.return_value = ([], [diagnostic])
            resource_runs.collect()
            self.assertEqual(render._malformed(), 0)
            self.assertEqual(resource_runs.collect.last_diagnostics, [diagnostic])

    def test_json_keeps_raw_resource_diagnostic_evidence(self):
        evidence = [{"kind": "resource-collector", "error": "PermissionError", "blocking": True}]
        value = json.loads(fleet._snapshot_json([], [], resource_diagnostics=evidence))
        self.assertEqual(value["resource_diagnostics"], evidence)

    def test_publication_keeps_resource_evidence_when_the_next_observation_changes(self):
        evidence = {"kind": "missing-registry", "path": "/audit/past-reference.json",
                    "reason": "registered-path-absent", "blocking": False}
        with tempfile.TemporaryDirectory() as td, ExitStack() as stack:
            stack.enter_context(mock.patch.dict(os.environ, {"XDG_STATE_HOME": td}))
            for target, result in (
                ("fleet.collectors.procscan.scan", []),
                ("fleet.collectors.herdr.list_agents", []),
                ("fleet.collectors.herdr.enrich", None),
                ("fleet.collectors.usage_cache.account_usage", {}),
                ("fleet.projection.attach_projections", None),
                ("fleet.session_tags.refresh", None),
                ("fleet.session_tags.assign", []),
                ("fleet.herdr_projection._report", None),
                ("fleet.collectors.dispatch.collect", []),
            ):
                stack.enter_context(mock.patch(target, return_value=result))
            dispatch.collect.last_malformed = 0
            reader = stack.enter_context(mock.patch.object(resource_runs, "collect", return_value=[]))
            reader.last_diagnostics = [evidence.copy()]
            published = collectors.collect_all(jobs_path=td + "/jobs", fast_first=True)
            reader.last_diagnostics[0]["path"] = "/audit/next-reference.json"
            collectors.collect_all.last_resource_diagnostics = reader.last_diagnostics
            self.assertEqual(published.resource_diagnostics, [evidence])
            self.assertEqual(published.malformed, 0)
            value = json.loads(fleet._snapshot_json([], [], resource_diagnostics=published.resource_diagnostics))
            self.assertEqual(value["resource_diagnostics"], [evidence])
            render.set_show_all(True)
            stack.enter_context(mock.patch.object(render, "_collect_governor", return_value=None))
            stack.enter_context(mock.patch.object(render, "_collect_memory", return_value=None))
            stack.enter_context(mock.patch.object(render, "_compute_host_rows", return_value=[]))
            output = io.StringIO()
            with redirect_stdout(output):
                render.render_once(lambda **kwargs: published, None, "both")
            self.assertIn("past-reference.json", output.getvalue())
            self.assertNotIn("next-reference.json", output.getvalue())

    def test_both_views_distinguish_past_references_from_unobserved_resources(self):
        missing = {"kind": "missing-registry", "path": "/work/project/old.json",
                   "reason": "registered-path-absent", "blocking": False}
        failure = {"kind": "resource-collector", "error": "PermissionError"}
        for process in (False, True):
            render.set_process_view(process)
            render.set_show_all(False)
            quiet = flatten(render._build_lines([], [], "both", False, 0,
                            term_width=80, resource_diagnostics=[missing]))
            self.assertNotIn("old.json", quiet)
            render.set_show_all(True)
            rows = render._build_lines([], [], "both", False, 2,
                           term_width=80, resource_diagnostics=[missing, failure])
            text = flatten(rows)
            for expected in ("jobs.log", "2", "missing reference", "nonblocking", 'resource unknown', "PermissionError"):
                self.assertIn(expected, text)
            self.assertNotIn("malformed jobs.log", text)
            self.assertTrue(all(render._dw("".join(t for t, _ in r)) <= 80
                                for r in render._diagnostic_rows([missing, failure], 2, 80)))

    def test_memory_collector_keeps_identity_and_origin(self):
        event = {"id": "record-1", "cwd": "/tmp/tmp-1", "scope": "project",
                 "project": "fixture", "ts": "2026-10-10T09:00:00", "snippet": "선택"}
        with tempfile.TemporaryDirectory() as td, mock.patch.dict(os.environ, {"MEM_WRITE_EVENTS": td + "/events"}):
            Path(td, "events").write_text(json.dumps(event) + "\n")
            result = memory.collect(datetime.datetime(2026, 10, 10, 12), include_summary=False)
        self.assertEqual(result["recent"][0], {**event, **{key: None for key in
                         ("action", "tier", "type", "actor", "sid")}})
        projected = next(iter(result["by_repo"].values()))[0]
        self.assertEqual(projected["id"], "record-1")
        self.assertEqual(projected["cwd"], "/tmp/tmp-1")

    def test_memory_content_and_origin_precede_storage_metadata_and_fit_width(self):
        events = [{"id": "r1", "cwd": "/tmp/tmp-first", "snippet": "[결정] 범위를 정해 주세요",
                   "ts": "2026-10-10T09:05:34", "action": "decision-record", "actor": "tidy-applier"},
                  {"id": "r2", "cwd": "/tmp/tmp-second", "snippet": "[결정] 범위를 정해 주세요",
                   "ts": "2026-10-10T09:05:35", "action": "decision-record", "actor": "tidy-applier"}]
        rows = render._mem_event_rows({"recent": events}, term_width=80)
        text = flatten(rows)
        self.assertEqual(len(rows), 2)
        for value in ("/tmp/tmp-first", "/tmp/tmp-second", 'mem saved', "범위를 정해 주세요"):
            self.assertIn(value, text)
        self.assertNotIn("tidy-applier", text)
        self.assertTrue(all(render._dw(flatten([row])) <= 80 for row in rows))
        self.assertIn('source unknown', flatten(render._mem_event_rows({"recent": [{"snippet": "내용"}]})))

    def test_legacy_delete_with_no_recorded_content_is_explicit(self):
        event = {"id": "r1", "cwd": "/work/project", "snippet": "",
                 "action": "delete", "actor": "manual", "tier": "working", "type": "decision"}
        text = flatten(render._mem_event_rows({"recent": [event]}, term_width=80))
        self.assertIn('mem pruned', text)
        self.assertIn('content unknown', text)
        self.assertIn("/work/project", text)
        self.assertNotIn("manual", text)

    def test_equal_changes_count_once_only_with_the_same_observed_source(self):
        event = {"id": "r1", "cwd": "/work/project", "snippet": "선택",
                 "action": "decision-record"}
        rows = render._mem_event_rows({"recent": [event, {**event, "id": "r2"}]})
        self.assertEqual(len(rows), 1)
        self.assertIn("×2", flatten(rows))
        self.assertEqual(render._mem_event_rows({"recent": [event]}, excluded_ids={"r1"}), [])

    def test_section_filter_never_hides_a_memory_change_or_displays_it_twice(self):
        event = {"id": "r1", "cwd": "/work/project", "snippet": "저장된 선택",
                 "action": "decision-record", "scope": "project"}
        observed = {"recent": [event], "by_repo": {"project": [event]}}
        session = Session(harness="claude", pid=1, cwd="/work/project", session_id="s1",
                          slug="fixture", title="fixture", liveness="idle")
        render.set_show_all(True)
        for section in ("fleet", "both", "dispatch"):
            with self.subTest(section=section):
                text = flatten(render._build_lines([session], [], section, False, 0,
                               memory=observed, term_width=120, governor=None))
                self.assertEqual(text.count("저장된 선택"), 1)


if __name__ == "__main__":
    unittest.main()
