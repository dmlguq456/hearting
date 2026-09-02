#!/usr/bin/env python3
"""F-97a/b/d/e — snapshot-owned location fields, suffix width, and the
attempt-scoped collector identity fix (dispatch-depth-1 owner survives a
same-slug depth-2 child instead of being clobbered by slug-keyed reconciliation).

f97e_jobs.log is a synthetic reconstruction of the shape described in the
frame direction-brief (§2.1) for jobs.log rows 331/335/338/339/341 — same
field layout, same dispatch_depth/attempt_id/parent linkage, same slug —
not a verbatim byte capture (that capture was not available to this test).
"""
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

_TOOLS_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)

from fleet import gitinfo, model, render  # noqa: E402
from fleet.collectors import dispatch  # noqa: E402
from fleet.model import DispatchJob  # noqa: E402

_FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "f97e_jobs.log")


def _ts(minutes_ago):
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()


def _rebased_fixture(tmp_dir):
    with open(_FIXTURE, encoding="utf-8") as fh:
        text = fh.read()
    text = (text
            .replace("__TS_331__", _ts(2))
            .replace("__TS_335__", _ts(3))
            .replace("__TS_338__", _ts(3))
            .replace("__TS_339__", _ts(3))
            .replace("__TS_341__", _ts(1)))
    path = os.path.join(tmp_dir, "jobs.log")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return path


class F97eOwnerSurvivalTest(unittest.TestCase):
    def test_open_depth1_owner_survives_same_slug_children(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _rebased_fixture(tmp)
            jobs, malformed = dispatch._scan_jobs_log(path, set())
        self.assertEqual(malformed, 0)
        owners = [j for j in jobs if j.depth == 1]
        self.assertEqual(len(owners), 1)
        self.assertEqual(owners[0].status, "open")
        self.assertGreaterEqual(len(jobs), 1)

    def test_owner_and_child_have_distinct_attempt_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _rebased_fixture(tmp)
            jobs, _malformed = dispatch._scan_jobs_log(path, set())
        attempts = {j.attempt_id for j in jobs}
        self.assertGreater(len(attempts), 1)

    def test_two_row_falsifier(self):
        """Minimal reproduction: an open dispatch-depth-1 owner and an open
        dispatch-depth-2 child sharing one slug must both survive."""
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "jobs.log")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("\t".join([
                    _ts(1), "open", "/repo/w", "/repo/w", "shared-slug",
                    "capability=autopilot-code,harness=claude,dispatch_depth=1,"
                    "worker_type=owner,attempt_id=att-owner-open",
                ]) + "\n")
                fh.write("\t".join([
                    _ts(1), "open", "/repo/w", "-", "shared-slug",
                    "capability=autopilot-code,harness=codex,dispatch_depth=2,"
                    "worker_type=child,parent=shared-slug,attempt_id=att-child-open",
                ]) + "\n")
            jobs, _malformed = dispatch._scan_jobs_log(path, set())
        self.assertEqual(len(jobs), 2)

    def test_legacy_row_without_attempt_id_still_slug_keyed(self):
        """290h-phantom regression guard: two rows sharing a slug with NO
        attempt_id at all must still collapse to the latest occurrence."""
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "jobs.log")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("\t".join([
                    _ts(300), "open", "/repo/w", "/repo/w", "legacy-slug",
                    "capability=autopilot-code,harness=claude,dispatch_depth=1",
                ]) + "\n")
                fh.write("\t".join([
                    _ts(1), "done", "/repo/w", "-", "legacy-slug",
                    "capability=autopilot-code,harness=claude,dispatch_depth=1",
                ]) + "\n")
            jobs, _malformed = dispatch._scan_jobs_log(path, set())
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].status, "done")


class F97aLocationKindTest(unittest.TestCase):
    def _job(self, cwd, parent_cwd, route_id=None):
        return DispatchJob(key="code", cwd=cwd, parent_cwd=parent_cwd, route_id=route_id)

    def test_kind_primary(self):
        job = self._job("/repo", "/repo")
        with mock.patch.object(gitinfo, "resolve_gitdir", return_value=("/repo/.git", "/repo/.git")):
            dispatch._fill_locations([job])
        self.assertEqual(job.location_kind, "primary")
        self.assertIsNone(job.location_repo)
        self.assertIsNone(job.location_wt)

    def test_kind_isolated_wt(self):
        job = self._job("/repo-wt/slug", "/repo")

        def _resolve(cwd):
            if cwd == "/repo-wt/slug":
                return ("/repo-wt/slug/.git", "/repo/.git")
            return ("/repo/.git", "/repo/.git")

        with mock.patch.object(gitinfo, "resolve_gitdir", side_effect=_resolve):
            dispatch._fill_locations([job])
        self.assertEqual(job.location_kind, "isolated-wt")
        self.assertEqual(job.location_wt, "slug")

    def test_kind_foreign_repo(self):
        job = self._job("/other-repo", "/repo")

        def _resolve(cwd):
            if cwd == "/other-repo":
                return ("/other-repo/.git", "/other-repo/.git")
            return ("/repo/.git", "/repo/.git")

        with mock.patch.object(gitinfo, "resolve_gitdir", side_effect=_resolve):
            dispatch._fill_locations([job])
        self.assertEqual(job.location_kind, "foreign-repo")
        self.assertEqual(job.location_repo, "other-repo")

    def test_kind_unknown_on_missing_parent_cwd(self):
        job = self._job("/repo", None)
        dispatch._fill_locations([job])
        self.assertEqual(job.location_kind, "unknown")

    def test_kind_unknown_on_resolve_error(self):
        job = self._job("/repo", "/repo")
        with mock.patch.object(gitinfo, "resolve_gitdir", side_effect=Exception("boom")):
            dispatch._fill_locations([job])
        self.assertEqual(job.location_kind, "unknown")

    def test_resolve_gitdir_called_once_per_unique_cwd(self):
        jobs = [self._job("/repo", "/repo"), self._job("/repo", "/repo"),
                self._job("/other", "/repo")]
        with mock.patch.object(gitinfo, "resolve_gitdir",
                               return_value=("/repo/.git", "/repo/.git")) as m:
            dispatch._fill_locations(jobs)
        self.assertEqual(m.call_count, 2)  # unique cwds: /repo, /other

    def test_json_carries_four_new_keys(self):
        d = DispatchJob(key="code").to_dict()
        for k in ("location_kind", "location_repo", "location_wt", "campaign_label"):
            self.assertIn(k, d)


class F97bSuffixWidthTest(unittest.TestCase):
    def test_repo_preserved_branch_clipped(self):
        segs = render._branch_suffix_segs(
            None, "peer-steward-fleet-location", dim=True,
            location_kind="foreign-repo", location_repo="hearting")
        text = "".join(t for t, _k in segs)
        self.assertIn("hearting", text)
        total_w = sum(render._dw(t) for t, _k in segs)
        self.assertLessEqual(total_w, render._BRANCH_SUFFIX_W + 1)

    def test_isolated_wt_token_dropped_when_budget_below_5(self):
        segs, w = render._location_prefix_segs("isolated-wt", None, 4)
        self.assertEqual(segs, [])
        self.assertEqual(w, 0)

    def test_no_location_token_without_branch_in_narrow(self):
        segs = render._branch_suffix_segs(None, None, optional=True,
                                          location_kind="foreign-repo", location_repo="hearting")
        self.assertEqual(segs, [])

    def test_primary_kind_has_no_location_token(self):
        segs = render._branch_suffix_segs(None, "main", location_kind="primary")
        self.assertFalse(any(k == "loc_repo" for _t, k in segs))

    def test_foreign_suffix_widths_overflow_zero(self):
        for width in (168, 120, 100, 60):
            segs = render._branch_suffix_segs(
                None, "peer-steward-fleet-location", dim=True,
                location_kind="foreign-repo", location_repo="hearting")
            total_w = sum(render._dw(t) for t, _k in segs)
            self.assertLessEqual(total_w, render._BRANCH_SUFFIX_W + 1,
                                 "width regression at terminal width %d" % width)

    def test_no_d1_d2_token_in_render(self):
        segs = render._branch_suffix_segs(
            None, "main", location_kind="isolated-wt")
        text = "".join(t for t, _k in segs)
        self.assertNotIn("d1", text)
        self.assertNotIn("d2", text)


class F97cCampaignLabelTest(unittest.TestCase):
    def test_campaign_label_exact_route_id_hit(self):
        with tempfile.TemporaryDirectory() as tmp:
            cycles_dir = os.path.join(tmp, ".runtime", "artifact-producer", "v1", "cycles")
            os.makedirs(cycles_dir)
            with open(os.path.join(cycles_dir, "cyc_1.json"), "w", encoding="utf-8") as fh:
                fh.write('{"route_id": "rt-abc", "title": "A very long campaign title indeed"}')
            job = DispatchJob(key="code", route_id="rt-abc", artifact_root=tmp)
            dispatch._campaign_labels([job])
        self.assertIsNotNone(job.campaign_label)
        self.assertLessEqual(len(job.campaign_label), 24)

    def test_campaign_label_miss_renders_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            cycles_dir = os.path.join(tmp, ".runtime", "artifact-producer", "v1", "cycles")
            os.makedirs(cycles_dir)
            with open(os.path.join(cycles_dir, "cyc_1.json"), "w", encoding="utf-8") as fh:
                fh.write('{"route_id": "rt-other", "title": "unrelated"}')
            job = DispatchJob(key="code", route_id="rt-abc", artifact_root=tmp)
            dispatch._campaign_labels([job])
        self.assertIsNone(job.campaign_label)

    def test_no_route_id_means_zero_file_io(self):
        job = DispatchJob(key="code", route_id=None, artifact_root="/nonexistent")
        with mock.patch("os.scandir") as m:
            dispatch._campaign_labels([job])
        m.assert_not_called()


if __name__ == "__main__":
    unittest.main()
