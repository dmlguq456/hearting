#!/usr/bin/env python3
"""Root read-entrance tests: determinism, ownership, read-only, rows, links, CLI."""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import contextlib
import hashlib
import io
import itertools
import contextlib
import copy
import io
import importlib.util
import shutil
import statistics
import time
from unittest import mock
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import artifact_entrance as E  # noqa: E402
import artifact_nofollow as nofollow  # noqa: E402

SERIAL = itertools.count(1)


def _digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:32]


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _tree(root: Path):
    files, dirs = {}, {}
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        if path.is_symlink():
            files[rel] = ("link", os.readlink(str(path)))
        elif path.is_file():
            stat = path.stat()
            files[rel] = (stat.st_size, stat.st_mtime_ns, _sha(path))
        elif path.is_dir():
            dirs[rel] = sorted(child.name for child in path.iterdir())
    return files, dirs


class EntranceFixture(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.root = self.base / "root"
        self.root.mkdir()
        (self.root / "campaigns").mkdir()
        (self.root / "campaigns" / "INDEX.md").write_text("# ID index\n", encoding="utf-8")
        self._env = {key: os.environ.get(key) for key in (
            "AGENT_ARTIFACT_ROOT", "FLEET_NOW_LANG", "XDG_CONFIG_HOME", "CLAUDE_CONFIG_DIR")}
        os.environ["AGENT_ARTIFACT_ROOT"] = str(self.root)
        os.environ.pop("FLEET_NOW_LANG", None)
        os.environ["XDG_CONFIG_HOME"] = str(self.base / "config")
        os.environ["CLAUDE_CONFIG_DIR"] = str(self.base / "claude")
        self.addCleanup(self._restore)

    def _restore(self):
        for key, value in self._env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self._tmp.cleanup()

    def cycle(self, campaign, locator, docs=(), *, title=None, sealed_on=None,
              started_on=None, audience=None, roles=None,
              state="completed", cycle_id=None, primary=None, extra_bytes=0):
        campaign_dir = self.root / "campaigns" / campaign
        campaign_dir.mkdir(parents=True, exist_ok=True)
        started_on = started_on or (locator[:10] + "T00:00:00Z")
        campaign_id = "camp_" + _digest("campaign:" + campaign)
        record_path = campaign_dir / "campaign.json"
        if not record_path.exists():
            record_path.write_text(json.dumps({"campaign_id": campaign_id, "locator": campaign}),
                                   encoding="utf-8")
        cycle_id = cycle_id or ("cyc_" + _digest(campaign + "/" + locator + "/" + str(next(SERIAL))))
        cycle_dir = campaign_dir / locator
        cycle_dir.mkdir(parents=True, exist_ok=True)
        (cycle_dir / ".cycle.json").write_text(json.dumps({
            "schema_version": 1, "kind": "artifact-cycle-binding",
            "campaign_id": campaign_id, "cycle_id": cycle_id, "started_on": started_on,
        }), encoding="utf-8")
        artifacts, revisions = [], []
        for rel, text in docs:
            path = cycle_dir / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
            artifact_id = "art_" + _digest(cycle_id + rel)
            row = {"artifact_id": artifact_id, "role": (roles or {}).get(rel, "output"),
                   "title": rel, "type": "output", "cycle_id": cycle_id}
            if audience is not None:
                value = audience.get(rel) if isinstance(audience, dict) else audience
                if value is not None:
                    row["audience"] = value
            artifacts.append(row)
            revisions.append({"artifact_id": artifact_id, "artifact_revision_id": "arev_" + _digest(rel),
                              "byte_size": len(text.encode("utf-8")),
                              "locator": {"kind": "cycle-relative", "path": rel},
                              "media_type": "text/markdown"})
        manifest = {"schema_version": 1,
                    "campaign": {"campaign_id": campaign_id},
                    "cycle": {"cycle_id": cycle_id, "started_on": started_on, "state": state},
                    "artifacts": artifacts, "artifact_revisions": revisions,
                    "manifest_id": "man_" + _digest(cycle_id + "manifest")}
        data = json.dumps(manifest)
        if primary is not None:
            data = data[:-1] + ', "primary": %s}' % json.dumps(primary)
        if extra_bytes:
            data += " " * extra_bytes
        (cycle_dir / "manifest.json").write_text(data, encoding="utf-8")
        record_dir = self.root / ".runtime" / "artifact-producer" / "v1" / "cycles"
        record_dir.mkdir(parents=True, exist_ok=True)
        (record_dir / f"{cycle_id}.json").write_text(json.dumps({
            "schema_version": 1, "cycle_id": cycle_id, "campaign_id": campaign_id,
            "title": title, "slug": locator.split("_", 1)[-1],
            "sealed_on": sealed_on, "state": "sealed",
        }), encoding="utf-8")
        return cycle_id

    def refresh(self, **kwargs):
        return E.refresh(self.root, **kwargs)

    def entrance(self, name="README.md") -> str:
        return (self.root / name).read_text(encoding="utf-8")

    def rows(self, limit=E.RECENT_LIMIT):
        return E.recent_rows(self.root, limit=limit)


class DeterminismTests(EntranceFixture):
    def test_second_refresh_is_unchanged_and_bytes_stay_same(self):
        self.cycle("2026-01-02_one", "2026-01-02_alpha",
                   [("artifacts/final_report.md", "# Report\n\n**한 줄**: everything passed.\n")])
        first = self.refresh()
        self.assertEqual(first["status"], "written")
        self.assertEqual(sorted(first["created"]), sorted(E.ENTRANCE_NAMES))
        before = {name: _sha(self.root / name) for name in E.ENTRANCE_NAMES}
        second = self.refresh()
        self.assertEqual(second["status"], "unchanged")
        self.assertEqual(second["created"], [])
        after = {name: _sha(self.root / name) for name in E.ENTRANCE_NAMES}
        self.assertEqual(before, after)

    def test_two_roots_with_the_same_content_render_identical_bytes(self):
        other = self.base / "second"
        for root in (self.root, other):
            campaign = root / "campaigns" / "2026-01-02_one"
            campaign.mkdir(parents=True, exist_ok=True)
            (campaign / "campaign.json").write_text(json.dumps({"campaign_id": "camp_" + _digest("campaign:2026-01-02_one")}),
                                                     encoding="utf-8")
            cycle_dir = campaign / "2026-01-02_alpha"
            (cycle_dir / "artifacts").mkdir(parents=True, exist_ok=True)
            (cycle_dir / "artifacts" / "final_report.md").write_text("# Shared\n\nSame bytes.\n", encoding="utf-8")
            (cycle_dir / ".cycle.json").write_text(json.dumps({
                "schema_version": 1, "kind": "artifact-cycle-binding",
                "campaign_id": "camp_" + _digest("campaign:2026-01-02_one"),
                "cycle_id": "cyc_" + _digest("shared-cycle"),
            }), encoding="utf-8")
            (cycle_dir / "manifest.json").write_text(json.dumps({
                "schema_version": 1,
                "campaign": {"campaign_id": "camp_" + _digest("campaign:2026-01-02_one")},
                "cycle": {"cycle_id": "cyc_" + _digest("shared-cycle"), "started_on": "2026-01-02T00:00:00Z"},
                "artifacts": [{"artifact_id": "art_" + _digest("doc"), "role": "output",
                               "title": "final", "type": "output"}],
                "artifact_revisions": [{"artifact_id": "art_" + _digest("doc"),
                                        "locator": {"kind": "cycle-relative", "path": "artifacts/final_report.md"}}],
            }), encoding="utf-8")
        E.refresh(self.root)
        E.refresh(other)
        for name in E.ENTRANCE_NAMES:
            self.assertEqual((self.root / name).read_bytes(), (other / name).read_bytes(), name)


class OwnershipTests(EntranceFixture):
    def test_human_owned_readme_is_kept_and_index_is_written(self):
        self.cycle("2026-01-02_one", "2026-01-02_alpha",
                   [("artifacts/final_report.md", "# Report\n\nBody.\n")])
        mine = "# My own notes\n\nDo not overwrite.\n"
        (self.root / "README.md").write_text(mine, encoding="utf-8")
        result = self.refresh()
        self.assertIn("README.md", result["foreign"])
        self.assertEqual((self.root / "README.md").read_text(encoding="utf-8"), mine)
        self.assertTrue((self.root / "index.html").is_file())
        self.assertEqual(self.refresh()["status"], "unchanged")

    def test_symlinked_target_is_never_followed(self):
        self.cycle("2026-01-02_one", "2026-01-02_alpha",
                   [("artifacts/final_report.md", "# Report\n\nBody.\n")])
        outside = self.base / "outside.md"
        outside.write_text("outside\n", encoding="utf-8")
        os.symlink(outside, self.root / "README.md")
        result = self.refresh()
        self.assertIn("README.md", result["foreign"])
        self.assertTrue((self.root / "README.md").is_symlink())
        self.assertEqual(outside.read_text(encoding="utf-8"), "outside\n")
        self.assertTrue((self.root / "index.html").is_file())

    def test_refresh_only_adds_entrance_names_and_touches_no_campaign_file(self):
        self.cycle("2026-01-02_one", "2026-01-02_alpha",
                   [("artifacts/final_report.md", "# Report\n\nBody.\n")])
        before_files, before_dirs = _tree(self.root)
        top_before = sorted(entry.name for entry in self.root.iterdir())
        E.refresh(self.root)
        after_files, after_dirs = _tree(self.root)
        top_after = sorted(entry.name for entry in self.root.iterdir())
        self.assertEqual(set(top_after) - set(top_before), set(E.ENTRANCE_NAMES))
        changed = {name for name, facts in after_files.items() if before_files.get(name) != facts}
        self.assertEqual(changed, set(E.ENTRANCE_NAMES))
        for name in E.ENTRANCE_NAMES:
            self.assertNotIn(os.path.join(str(self.root), name), before_files)
        self.assertEqual({key: value for key, value in before_dirs.items()},
                         {key: value for key, value in after_dirs.items()
                          if key in before_dirs})
        self.assertFalse(any(path.is_symlink() for path in self.root.rglob("*")))


class RowRuleTests(EntranceFixture):
    def test_audience_key_wins_and_old_manifests_use_the_name_rule(self):
        explicit = self.cycle("2026-01-03_explicit", "2026-01-03_alpha",
                              [("artifacts/final_report.md", "# Explicit\n\nBody.\n")],
                              audience="user", sealed_on="2026-01-03T00:00:00Z")
        old = self.cycle("2026-01-02_old", "2026-01-02_beta",
                         [("artifacts/plans/final_report.md", "# Old\n\nBody.\n")],
                         sealed_on="2026-01-02T00:00:00Z")
        snapshot = E.scan(self.root)
        by_id = {row["cycle_id"]: row for row in snapshot["recent"]}
        self.assertEqual(by_id[explicit]["selected_by"], "audience")
        self.assertEqual(by_id[old]["selected_by"], "name-rule")
        self.assertEqual(snapshot["selected_by"], {"audience": 1, "name-rule": 1})
        manifest = self.root / "campaigns" / "2026-01-02_old" / "2026-01-02_beta" / "manifest.json"
        self.assertNotIn('"audience"', json.loads(manifest.read_text(encoding="utf-8"))["artifacts"][0])

    def test_explicit_non_user_audience_and_support_rows_are_excluded(self):
        self.cycle("2026-01-02_one", "2026-01-02_alpha", [
            ("artifacts/final_report.md", "# Report\n\nBody.\n"),
            ("artifacts/spec/prd.md", "# PRD\n\nBody.\n"),
        ], audience={"artifacts/final_report.md": "engineer"}, roles={"artifacts/spec/prd.md": "support"})
        self.assertEqual(E.recent_rows(self.root), [])

    def test_record_folder_documents_are_excluded(self):
        self.cycle("2026-01-02_one", "2026-01-02_alpha", [
            ("artifacts/dev_logs/final_report.md", "# Log\n\nBody.\n"),
            ("artifacts/_internal/summary.md", "# Support\n\nBody.\n"),
        ])
        self.assertEqual(E.recent_rows(self.root), [])

    def test_representative_rank_ignores_manifest_primary(self):
        self.cycle("2026-01-02_one", "2026-01-02_alpha", [
            ("artifacts/현황.md", "# 현황\n\nStatus body.\n"),
            ("artifacts/final_report.md", "# Final\n\nReport body.\n"),
        ], primary="artifacts/final_report.md")
        rows = E.recent_rows(self.root)
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["doc_path"].endswith("artifacts/현황.md"))
        self.assertEqual(rows[0]["extra"], 1)

    def test_generic_heading_falls_back_to_the_cycle_title(self):
        self.cycle("2026-01-02_one", "2026-01-02_alpha",
                   [("artifacts/final_report.md", "# final report\n\nBody.\n")],
                   title="읽을 만한 제목")
        self.assertEqual(E.recent_rows(self.root)[0]["title"], "읽을 만한 제목")

    def test_summary_prefers_the_one_line_and_skips_meta_lines(self):
        self.cycle("2026-01-02_one", "2026-01-02_alpha",
                   [("artifacts/final_report.md",
                     "---\ncreated: 2026-01-02\n---\n# Report\n\n"
                     "- **검증**: PASS\n| a | b |\n\n첫 문단 첫 줄\n둘째 줄.\n")])
        self.assertEqual(E.recent_rows(self.root)[0]["summary"], "첫 문단 첫 줄 둘째 줄.")

    def test_summary_uses_the_one_line_form(self):
        self.cycle("2026-01-02_one", "2026-01-02_alpha",
                   [("artifacts/final_report.md", "# Report\n\n**한 줄**: 짧은 결과.\n\n본문.\n")])
        self.assertEqual(E.recent_rows(self.root)[0]["summary"], "짧은 결과.")

    def test_summary_is_cut_at_one_hundred_twenty_code_points(self):
        long = "가" * 200
        self.cycle("2026-01-02_one", "2026-01-02_alpha",
                   [("artifacts/final_report.md", f"# Report\n\n{long}\n")])
        summary = E.recent_rows(self.root)[0]["summary"]
        self.assertEqual(len(summary), E.SUMMARY_CHARS + 1)
        self.assertTrue(summary.endswith("…"))

    def test_recent_order_is_date_descending_then_cycle_id_ascending(self):
        late = self.cycle("2026-01-05_late", "2026-01-05_alpha",
                          [("artifacts/final_report.md", "# Late\n\nBody.\n")],
                          sealed_on="2026-01-05T00:00:00Z")
        tie_a = "cyc_" + "0" * 32
        tie_b = "cyc_" + "f" * 32
        self.cycle("2026-01-04_tie", "2026-01-04_alpha",
                   [("artifacts/final_report.md", "# Tie B\n\nBody.\n")],
                   sealed_on="2026-01-04T00:00:00Z", cycle_id=tie_b)
        self.cycle("2026-01-04_tie", "2026-01-04_beta",
                   [("artifacts/final_report.md", "# Tie A\n\nBody.\n")],
                   sealed_on="2026-01-04T00:00:00Z", cycle_id=tie_a)
        rows = self.rows()
        self.assertEqual([row["cycle_id"] for row in rows], [late, tie_a, tie_b])


class CyclePathTests(EntranceFixture):
    def _bad_locator(self, attack):
        self.cycle("2026-01-02_one", "2026-01-02_alpha",
                   [("artifacts/final_report.md", "# Safe\n\nBody.\n")])
        cycle = self.root / "campaigns/2026-01-02_one/2026-01-02_alpha"
        outside = self.base / "outside"
        outside.mkdir()
        (outside / "final_report.md").write_text("# OUTSIDE ROOT\n\nPrivate.\n")
        document = cycle / "artifacts/final_report.md"
        if attack == "traversal":
            locator = "../../../../outside/final_report.md"
        elif attack == "parent-link":
            (cycle / "artifacts/jump").symlink_to(outside, target_is_directory=True)
            locator = "artifacts/jump/final_report.md"
        elif attack == "leaf-link":
            document.unlink()
            document.symlink_to(outside / "final_report.md")
            locator = "artifacts/final_report.md"
        else:
            locator = str(outside / "final_report.md")
        manifest = cycle / "manifest.json"
        value = json.loads(manifest.read_text())
        value["artifact_revisions"][0]["locator"]["path"] = locator
        manifest.write_text(json.dumps(value))

    def _assert_rejected(self, attack):
        self._bad_locator(attack)
        before = _tree(self.base)
        with mock.patch.object(E, "_read_head", wraps=E._read_head) as read:
            self.assertEqual(self.rows(), [])
        read.assert_not_called()
        self.assertEqual(_tree(self.base), before)
        self.refresh()
        for name in E.ENTRANCE_NAMES:
            self.assertNotIn("OUTSIDE ROOT", self.entrance(name))
            self.assertNotIn("outside/final_report.md", self.entrance(name))
        with mock.patch.object(E, "_open_externally") as opener, \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(E._command_open(self.root, ["final_report.md"]), 1)
        opener.assert_not_called()

    def test_traversal_locator_never_reads_links_or_opens_outside(self):
        self._assert_rejected("traversal")

    def test_parent_symlink_never_reads_links_or_opens_outside(self):
        self._assert_rejected("parent-link")

    def test_leaf_symlink_never_reads_links_or_opens_outside(self):
        self._assert_rejected("leaf-link")

    def test_absolute_locator_never_reads_links_or_opens_outside(self):
        self._assert_rejected("absolute")

    def test_open_rechecks_a_symlink_swapped_after_scan(self):
        self.cycle("2026-01-02_one", "2026-01-02_alpha",
                   [("artifacts/final_report.md", "# Safe\n\nBody.\n")])
        rows = self.rows()
        document = self.root / rows[0]["doc_path"]
        outside = self.base / "private.md"
        outside.write_text("# Private\n")
        def swapped(_root):
            document.unlink()
            document.symlink_to(outside)
            return {}, {"recent": rows, "topics": [], "campaigns": [], "all_campaigns": []}
        with mock.patch.object(E, "_refresh_and_snapshot", side_effect=swapped), \
                mock.patch.object(E, "_open_externally") as opener, \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(E._command_open(self.root, ["Safe"]), 1)
        opener.assert_not_called()

    def _swap_after_check(self, swap):
        """Run `swap` once, right after _document_path accepts a document and before it is read."""
        original = E._document_path
        done = []

        def checked(*args, **kwargs):
            found = original(*args, **kwargs)
            if found is not None and not done:
                done.append(found)
                swap(found)
            return found
        return mock.patch.object(E, "_document_path", side_effect=checked), done

    def _assert_swap_unread(self, swap):
        self.cycle("2026-01-02_one", "2026-01-02_alpha",
                   [("artifacts/final_report.md", "# Safe\n\nBody.\n")])
        patch, done = self._swap_after_check(swap)
        with patch:
            self.refresh()
        self.assertTrue(done)
        for name in E.ENTRANCE_NAMES:
            self.assertNotIn("OUTSIDE_SENTINEL", self.entrance(name))

    def test_leaf_swapped_for_a_link_after_the_check_is_never_read(self):
        outside = self.base / "private.md"
        outside.write_text("# OUTSIDE_SENTINEL title\n\nOUTSIDE_SENTINEL summary.\n")

        def swap(document):
            document.unlink()
            document.symlink_to(outside)
        self._assert_swap_unread(swap)

    def test_ancestor_swapped_for_a_link_after_the_check_is_never_read(self):
        outside = self.base / "outside-dir"
        outside.mkdir()
        (outside / "final_report.md").write_text("# OUTSIDE_SENTINEL title\n\nOUTSIDE_SENTINEL summary.\n")

        def swap(document):
            parent = document.parent
            parent.rename(parent.with_name("artifacts-old"))
            parent.symlink_to(outside, target_is_directory=True)
        self._assert_swap_unread(swap)

    def test_head_is_empty_without_the_no_follow_primitives(self):
        self.cycle("2026-01-02_one", "2026-01-02_alpha",
                   [("artifacts/final_report.md", "# Safe\n\nBody.\n")])
        self.assertEqual(self.rows()[0]["title"], "Safe")
        with mock.patch.object(nofollow, "check_nofollow_platform", side_effect=RuntimeError("no")):
            row = self.rows()[0]
        self.assertEqual(row["summary"], "")
        self.assertNotEqual(row["title"], "Safe")

    def test_cycle_and_source_rows_share_containment_and_keep_normal_rows(self):
        self.cycle("2026-01-02_one", "2026-01-02_alpha",
                   [("artifacts/final_report.md", "# Safe\n\nBody.\n")])
        (self.root / "note.md").write_text("# Note\n")
        outside = self.base / "outside"
        outside.mkdir()
        (outside / "note.md").write_text("# Private\n")
        (self.root / "jump").symlink_to(outside)
        source = lambda _root: [dict(kind="note", date="2026-01-03", title="Note", doc_path=doc)
                               for doc in ("../outside/note.md", "jump/note.md", "note.md")]
        with mock.patch.object(E, "ROW_SOURCES", (source,)):
            rows = self.rows()
            self.assertEqual([row["kind"] for row in rows], ["note", "cycle"])
            self.assertEqual(set(rows[0]), set(rows[1]))
            self.refresh()
            before = {name: (self.root / name).read_bytes() for name in E.ENTRANCE_NAMES}
            self.assertEqual(self.refresh()["status"], "unchanged")
            self.assertEqual(before, {name: (self.root / name).read_bytes() for name in E.ENTRANCE_NAMES})
            with mock.patch.object(E, "_open_externally") as opener, \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(E._command_open(self.root, ["Safe"]), 0)
            opener.assert_called_once_with(self.root / rows[1]["doc_path"])

    def test_placed_output_is_revalidated(self):
        self.cycle("2026-01-02_one", "2026-01-02_alpha",
                   [("artifacts/final_report.md", "# Safe\n\nBody.\n")])
        outside = self.base / "final_report.md"
        outside.write_text("# Private\n")
        with mock.patch.object(E, "resolve_placed_output", return_value=outside):
            self.assertEqual(self.rows(), [])
        moved = self.root / "moved.md"
        moved.write_text("# Moved\n")
        with mock.patch.object(E, "resolve_placed_output", return_value=moved):
            self.assertEqual(self.rows()[0]["title"], "Moved")


class RefreshWorkerTests(EntranceFixture):
    def test_slow_refresh_coalesces_concurrent_requests_and_exits(self):
        self.cycle("2026-01-02_one", "2026-01-02_alpha",
                   [("artifacts/final_report.md", "# Safe\n\nBody.\n")])
        release = self.base / "release"
        started = self.base / "started"
        calls = self.base / "calls"
        children = []
        popen = subprocess.Popen
        injected = ("import time\noriginal = e.refresh\n"
                    "def slow(root, cycle_id=None):\n"
                    f"    with open({str(calls)!r}, 'a') as log: log.write(str(cycle_id) + '\\n')\n"
                    f"    Path({str(started)!r}).touch()\n"
                    "    deadline = time.monotonic() + 10\n"
                    f"    while not Path({str(release)!r}).exists():\n"
                    "        if time.monotonic() > deadline: raise RuntimeError('fixture deadline')\n"
                    "        time.sleep(0.01)\n"
                    "    return original(root, cycle_id=cycle_id)\n"
                    "e.refresh = slow\n")
        def spawn(args, **kwargs):
            args = list(args)
            args[2] = args[2].replace("e._refresh_requested(", f"exec({injected!r}); e._refresh_requested(")
            child = popen(args, **kwargs)
            children.append(child)
            return child
        def settle():
            release.touch()
            for child in children:
                try:
                    child.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=5)
        self.addCleanup(settle)
        with mock.patch.object(E, "in_test_process", return_value=False), \
                mock.patch.object(E.subprocess, "Popen", side_effect=spawn):
            start = time.monotonic()
            E.request_refresh(self.root, cycle_id="first")
            self.assertLess(time.monotonic() - start, 1.5)
            deadline = time.monotonic() + 5
            while not started.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(started.exists())
            barrier = threading.Barrier(16)
            errors = []
            def request():
                try:
                    barrier.wait(timeout=5)
                    E.request_refresh(self.root, cycle_id="follow-up")
                except Exception as exc:
                    errors.append(exc)
            threads = [threading.Thread(target=request) for _ in range(16)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=5)
            self.assertFalse(errors)
            self.assertTrue(all(not thread.is_alive() for thread in threads))
            self.assertEqual(len(children), 1)
            self.assertFalse((self.root / "README.md").exists())
            release.touch()
            self.assertEqual(children[0].wait(timeout=10), 0)
        self.assertEqual(calls.read_text().splitlines(), ["first", "follow-up"])
        self.assertTrue((self.root / "README.md").is_file())
        self.assertFalse((self.root / ".runtime/artifact-entrance-pending.json").exists())
        # A completed worker leaves no held lease or idle process.
        with mock.patch.object(E, "in_test_process", return_value=False), \
                mock.patch.object(E.subprocess, "Popen", side_effect=spawn):
            E.request_refresh(self.root, cycle_id="new-worker")
            self.assertEqual(children[-1].wait(timeout=10), 0)
        self.assertEqual(len(children), 2)

    def test_refresh_failure_releases_worker_for_the_next_request(self):
        self.cycle("2026-01-02_one", "2026-01-02_alpha",
                   [("artifacts/final_report.md", "# Safe\n\nBody.\n")])
        children = []
        popen = subprocess.Popen
        def spawn(args, **kwargs):
            args = list(args)
            if not children:
                injected = "def broken(*args, **kwargs): raise RuntimeError('fixture refresh failure')\ne.refresh = broken\n"
                args[2] = args[2].replace("e._refresh_requested(", f"exec({injected!r}); e._refresh_requested(")
            child = popen(args, **kwargs)
            children.append(child)
            return child
        def settle():
            for child in children:
                try:
                    child.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=5)
        self.addCleanup(settle)
        with mock.patch.object(E, "in_test_process", return_value=False), \
                mock.patch.object(E.subprocess, "Popen", side_effect=spawn):
            E.request_refresh(self.root)
            self.assertEqual(children[-1].wait(timeout=10), 0)
            self.assertFalse((self.root / "README.md").exists())
            E.request_refresh(self.root)
            self.assertEqual(children[-1].wait(timeout=10), 0)
        self.assertEqual(len(children), 2)
        self.assertTrue((self.root / "README.md").is_file())


class RowSourceTests(EntranceFixture):
    """`ROW_SOURCES` is the only door for rows that are not cycle folders."""

    def _plug(self, *sources):
        saved = E.ROW_SOURCES
        E.ROW_SOURCES = tuple(sources)
        self.addCleanup(lambda: setattr(E, "ROW_SOURCES", saved))

    def _note(self, rel="extra/note.md", text="# Note\n\nBody.\n"):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return rel

    def _two_cycles(self):
        self.cycle("2026-01-02_one", "2026-01-02_alpha",
                   [("artifacts/final_report.md", "# Alpha\n\nBody.\n")], sealed_on="2026-01-02T00:00:00Z")
        self.cycle("2026-01-06_two", "2026-01-06_beta",
                   [("artifacts/final_report.md", "# Beta\n\nBody.\n")], sealed_on="2026-01-06T00:00:00Z")

    def test_default_sources_are_empty_without_journals_or_campaign_documents_and_change_no_byte(self):
        self.assertEqual(E.ROW_SOURCES, (E._daily_journals, E._campaign_documents, E._canonical_topics))
        self._two_cycles()
        self.refresh()
        names = E.ENTRANCE_NAMES
        before = {name: (self.root / name).read_bytes() for name in names}
        self.assertTrue(all(row["kind"] == "cycle" for row in self.rows()))
        self._plug(lambda root: [])
        self.assertEqual(self.refresh()["status"], "unchanged")
        self.assertEqual({name: (self.root / name).read_bytes() for name in names}, before)

    def test_a_source_row_joins_the_route_reports_page_and_open_in_date_order(self):
        self._two_cycles()
        rel = self._note()
        self._plug(lambda root: [{"kind": "note", "date": "2026-01-04", "title": "Day note",
                                  "summary": "  one   line ", "doc_path": rel}])
        self.refresh()
        rows = self.rows()
        self.assertEqual([row["date"] for row in rows], ["2026-01-06", "2026-01-04", "2026-01-02"])
        self.assertEqual(rows[1]["kind"], "note")
        self.assertEqual((rows[1]["cycle_id"], rows[1]["cycle_path"], rows[1]["extra"]), ("", "", 0))
        self.assertEqual(rows[1]["summary"], "one line")
        readme, page = self.entrance(E.RECORDS_MD), self.entrance(E.RECORDS_HTML)
        self.assertIn("- 2026-01-04  [Day note](<extra/note.md>)\n   one line", readme)
        self.assertNotIn("``", readme)
        self.assertIn('<a href="extra/note.md">Day note</a>', page)
        self.assertNotIn("note</span>", page.replace("Day note", ""))
        self.assertNotIn("Day note", self.entrance())  # the entrance's top no longer lists route reports
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(E._command_open(self.root, ["Day", "note"]), 0)
        self.assertEqual(out.getvalue().strip(), str(self.root / rel))

    def test_same_date_order_is_deterministic_with_mixed_rows(self):
        self._two_cycles()
        a, b = self._note("x/a.md"), self._note("x/b.md")
        row = lambda rel: {"kind": "note", "date": "2026-01-06", "title": rel, "doc_path": rel}
        self._plug(lambda root: [row(b), row(a)])
        first = [r["doc_path"] for r in self.rows()]
        self._plug(lambda root: [row(a), row(b)])
        self.assertEqual([r["doc_path"] for r in self.rows()], first)
        self.assertEqual(len(first), 4)

    def test_invalid_rows_are_skipped(self):
        self._two_cycles()
        rel = self._note()
        (self.root / "extra" / "link.md").symlink_to(self.root / rel)
        (self.root / "linkdir").symlink_to(self.root / "extra")
        base = {"kind": "note", "date": "2026-01-04", "title": "T", "doc_path": rel}
        bad = [
            {k: v for k, v in base.items() if k != "title"},
            {k: v for k, v in base.items() if k != "date"},
            {k: v for k, v in base.items() if k != "kind"},
            {k: v for k, v in base.items() if k != "doc_path"},
            dict(base, kind=""),
            dict(base, kind="cycle"),
            dict(base, date="2026-1-4"),
            dict(base, title="  "),
            dict(base, doc_path=str(self.root / rel)),
            dict(base, doc_path="extra/../extra/note.md"),
            dict(base, doc_path="../root/extra/note.md"),
            dict(base, doc_path="extra/link.md"),
            dict(base, doc_path="linkdir/note.md"),
            dict(base, doc_path="extra/missing.md"),
            dict(base, doc_path="extra"),
            "not a mapping",
        ]
        self._plug(lambda root: bad)
        self.assertEqual([row["kind"] for row in self.rows()], ["cycle", "cycle"])
        self._plug(lambda root: bad + [base])
        self.assertEqual(sorted(row["kind"] for row in self.rows()), ["cycle", "cycle", "note"])

    def test_a_raising_source_is_skipped_and_the_rest_renders(self):
        self._two_cycles()
        rel = self._note()

        def boom(root):
            raise RuntimeError("source failed")

        def lazy(root):
            yield {"kind": "note", "date": "2026-01-04", "title": "Late", "doc_path": rel}
            raise RuntimeError("fails after the first row")

        self._plug(boom, lambda root: [{"kind": "note", "date": "2026-01-03", "title": "Kept", "doc_path": rel}], lazy)
        self.assertEqual(self.refresh()["status"], "written")
        self.assertEqual([row["title"] for row in self.rows()], ["Beta", "Kept", "Alpha"])
        self.assertIn("Kept", self.entrance(E.RECORDS_MD))


class LimitTests(EntranceFixture):
    def test_recent_list_stops_at_twenty_and_the_entrance_campaigns_at_three(self):
        for index in range(30):
            day = f"2026-02-{index + 1:02d}"
            campaign = f"{day}_campaign-{index:02d}" if index < 12 else "2026-01-01_shared"
            locator = f"{day}_cycle-{index:02d}"
            self.cycle(campaign, locator, [("artifacts/final_report.md", f"# Doc {index}\n\nBody.\n")],
                       sealed_on=f"{day}T00:00:00Z")
        snapshot = E.scan(self.root)
        self.assertEqual(len(snapshot["recent"]), E.RECENT_LIMIT)
        self.assertEqual(len(snapshot["campaigns"]), E.ENTRANCE_CAMPAIGN_LINES)
        self.assertEqual(E.ENTRANCE_CAMPAIGN_LINES, 3)
        self.assertEqual(len(snapshot["all_campaigns"]), 13)
        # another caller keeps its own, unchanged line count
        self.assertEqual(E.CAMPAIGN_LIMIT, 10)
        self.assertEqual(len(E.campaign_groups(self.root)), 10)

    def test_an_oversized_manifest_is_skipped(self):
        self.cycle("2026-01-02_big", "2026-01-02_big",
                   [("artifacts/final_report.md", "# Big\n\nBody.\n")], extra_bytes=4096)
        self.cycle("2026-01-03_ok", "2026-01-03_ok",
                   [("artifacts/final_report.md", "# Ok\n\nBody.\n")])
        original = E.MAX_MANIFEST_BYTES
        E.MAX_MANIFEST_BYTES = 1024
        try:
            rows = E.recent_rows(self.root)
        finally:
            E.MAX_MANIFEST_BYTES = original
        self.assertEqual([row["title"] for row in rows], ["Ok"])

    def test_all_campaigns_page_lists_every_campaign(self):
        for index in range(12):
            day = f"2026-03-{index + 1:02d}"
            self.cycle(f"{day}_campaign-{index:02d}", f"{day}_cycle", [
                ("artifacts/final_report.md", f"# Doc {index}\n\nBody.\n")])
        self.refresh()
        page = self.entrance("all-campaigns.md")
        self.assertEqual(len(re.findall(r"^[-*] \[.*?\]\(<campaigns/", page, re.MULTILINE)), 12)
        entrance = self.entrance()
        campaign_items = re.findall(r"^\d+\. .*\]\(<campaigns/\d{4}", entrance, re.MULTILINE)
        self.assertEqual(len(campaign_items), E.ENTRANCE_CAMPAIGN_LINES)
        self.assertIn("[All 12 campaigns](<all-campaigns.md>)", entrance)
        self.assertTrue(page.startswith(E.MARKER + "\n# All campaigns\n"))  # the page's title is unchanged


class LinkTests(EntranceFixture):
    def test_literal_filename_characters_open_exact_documents_in_html(self):
        class Links(HTMLParser):
            def __init__(self):
                super().__init__(); self.hrefs = []

            def handle_starttag(self, tag, attrs):
                if tag == "a":
                    self.hrefs.extend(value for key, value in attrs if key == "href")

        paths = []
        for number, name in enumerate(("report#done.md", "report%20done.md", "보고서 #100%25.md")):
            locator = f"2026-11-06_document-{number}"
            self.cycle("2026-11-06_example", locator, [("artifacts/" + name, "report")], audience="user")
            paths.append(self.root / "campaigns/2026-11-06_example" / locator / "artifacts" / name)
        links = Links()
        snapshot = E.scan(self.root)
        self.assertEqual({self.root / row["doc_path"] for row in snapshot["recent"]}, set(paths))
        links.feed(E.render_recent_html(snapshot, E._translator()))
        opened = {self.root / unquote(urlsplit(href).path) for href in links.hrefs
                  if not urlsplit(href).scheme}
        for path in paths:
            with self.subTest(filename=path.name):
                self.assertIn(path, opened)

    def test_every_relative_link_of_all_four_files_exists(self):
        self.cycle("2026-01-02_한글 캠페인", "2026-01-02_보고서 #1 100%",
                   [("artifacts/보고서 #1 100%.md", "# 이름 있는 문서\n\nBody.\n")])
        self.refresh()
        for name in E.ENTRANCE_NAMES:
            text = self.entrance(name)
            targets = re.findall(r"\]\(<(.*?)>\)", text) if name.endswith(".md") else []
            if name.endswith(".html"):
                targets = re.findall(r'href="([^"]+)"', text)
            for target in targets:
                from urllib.parse import unquote
                self.assertNotIn("#", target, name)
                decoded = unquote(target)
                path = self.root / decoded
                self.assertTrue(path.exists(), f"{name} -> {target}")


class LanguageTests(EntranceFixture):
    def test_declared_korean_and_the_english_default(self):
        self.cycle("2026-01-02_one", "2026-01-02_alpha",
                   [("artifacts/final_report.md", "# Report\n\nBody.\n")])
        E.refresh(self.root)
        self.assertIn("## Campaigns", self.entrance())
        self.assertIn("# Recent route reports", self.entrance(E.RECORDS_MD))
        os.environ["FLEET_NOW_LANG"] = "ko"
        E.refresh(self.root)
        self.assertIn("## 캠페인", self.entrance())
        self.assertIn("## 기록", self.entrance())
        self.assertIn("# 최근 route 보고", self.entrance(E.RECORDS_MD))
        self.assertIn('lang="ko"', self.entrance("index.html"))
        self.assertIn('lang="ko"', self.entrance(E.RECORDS_HTML))


class CliTests(EntranceFixture):
    def _run(self, *args, tty=False):
        env = dict(os.environ)
        env["AGENT_ARTIFACT_ROOT"] = str(self.root)
        env.pop("FLEET_NOW_LANG", None)
        return subprocess.run([sys.executable, str(HERE / "artifact_entrance.py"), *args],
                              capture_output=True, text=True, env=env,
                              stdin=subprocess.DEVNULL)

    def _three_documents(self):
        self.cycle("2026-01-02_one", "2026-01-02_alpha",
                   [("artifacts/final_report.md", "# Alpha result\n\nFirst body.\n")])
        self.cycle("2026-01-03_two", "2026-01-03_beta",
                   [("artifacts/final_report.md", "# Beta result\n\nSecond body.\n")])
        self.cycle("2026-01-04_three", "2026-01-04_gamma",
                   [("artifacts/final_report.md", "# Gamma result\n\nThird body.\n")])

    def test_reports_numbering_matches_the_entrance(self):
        self._three_documents()
        done = self._run("reports")
        self.assertEqual(done.returncode, 0, done.stderr)
        lines = [line for line in done.stdout.splitlines() if re.match(r"^\d+\. ", line)]
        # three campaigns, the all-campaigns page, the route reports page, the ID index
        self.assertEqual(len(lines), 6)
        self.assertIn("1. 2026-01-04", lines[0])
        entrance = self.entrance()
        self.assertIn("1. 2026-01-04", entrance)
        self.assertIn("3. 2026-01-02", entrance)
        self.assertIn(str(self.root / "README.md"), done.stdout)

    def test_open_number_prints_that_entrance_line_target(self):
        self._three_documents()
        done = self._run("open", "2")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout.strip(), str(self.root / "campaigns/2026-01-03_two"))

    def test_open_with_no_argument_prints_the_browser_entrance(self):
        self._three_documents()
        done = self._run("open")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout.strip(), str(self.root / "index.html"))

    def test_open_search_one_none_and_several(self):
        self._three_documents()
        one = self._run("open", "gamma")
        self.assertEqual(one.returncode, 0, one.stderr)
        self.assertTrue(one.stdout.strip().endswith("2026-01-04_gamma/artifacts/final_report.md"))
        none = self._run("open", "없는이름")
        self.assertEqual(none.returncode, 1)
        self.assertEqual(none.stdout.strip(), "")
        several = self._run("open", "result")
        self.assertEqual(several.returncode, 1)
        self.assertEqual(len([line for line in several.stdout.splitlines() if re.match(r"^\d{4}-", line)]), 3)
        self.assertEqual([line for line in several.stdout.splitlines() if re.match(r"^\d+\. ", line)], [])
        self.assertIn("add more words", several.stderr)

    def test_an_out_of_range_number_says_so_and_is_never_searched_as_a_word(self):
        self._three_documents()
        done = self._run("open", "99")
        self.assertEqual(done.returncode, 1)
        self.assertEqual(done.stdout.strip(), "")
        self.assertEqual(done.stderr.strip(), "There is no line 99. The entrance has lines 1 to 6.")

    def test_a_non_tty_never_tries_to_open_a_window(self):
        self._three_documents()
        done = self._run("open", "1")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stderr.strip(), "")


class FirstCycleRecordTests(EntranceFixture):
    def test_only_a_cycle_in_the_recent_list_is_recorded_once(self):
        support_only = self.cycle("2026-01-01_support", "2026-01-01_support",
                                  [("artifacts/dev_logs/run.md", "# Log\n\nBody.\n")])
        self.assertEqual(self.refresh(cycle_id=support_only)["status"], "written")
        self.assertFalse((self.root / E.FIRST_RECORD_REL).exists())
        first = self.cycle("2026-01-02_first", "2026-01-02_alpha",
                           [("artifacts/final_report.md", "# First\n\nBody.\n")])
        self.refresh(cycle_id=first)
        record = json.loads((self.root / E.FIRST_RECORD_REL).read_text(encoding="utf-8"))
        self.assertEqual(record["first_cycle_id"], first)
        self.assertEqual(record["first_cycle_path"], "campaigns/2026-01-02_first/2026-01-02_alpha")
        second = self.cycle("2026-01-03_second", "2026-01-03_beta",
                            [("artifacts/final_report.md", "# Second\n\nBody.\n")])
        self.refresh(cycle_id=second)
        self.assertEqual(json.loads((self.root / E.FIRST_RECORD_REL).read_text(encoding="utf-8")),
                         record)

    def test_no_cycle_id_writes_no_record(self):
        self.cycle("2026-01-02_one", "2026-01-02_alpha",
                   [("artifacts/final_report.md", "# Report\n\nBody.\n")])
        self.refresh()
        self.assertFalse((self.root / E.FIRST_RECORD_REL).exists())

    def test_a_missing_root_is_reported_not_created(self):
        missing = self.base / "absent"
        result = E.refresh(missing)
        self.assertEqual(result["status"], "skipped-no-root")
        self.assertFalse(missing.exists())



class EntranceIntegrationTests(EntranceFixture):
    def test_one_scan_commands_and_reuse(self):
        self.cycle("2026-01-02_one", "2026-01-02_alpha", [("artifacts/report.md", "# Hello\ntext")])
        with mock.patch.object(E, 'scan', wraps=E.scan) as scan, contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            E._command_reports(self.root)
            self.assertEqual(scan.call_count, 1)
            scan.reset_mock(); E._command_open(self.root, [])
            self.assertEqual(scan.call_count, 1)
            scan.reset_mock(); E._command_open(self.root, ['not-found'])
            self.assertEqual(scan.call_count, 1)
        with mock.patch.object(E, 'render_all_markdown', wraps=E.render_all_markdown) as md, mock.patch.object(E, 'render_all_html', wraps=E.render_all_html) as ht:
            before = [(self.root/n).read_bytes() for n in ('all-campaigns.md', 'all-campaigns.html')]
            E.refresh(self.root)
            self.assertEqual((md.call_count, ht.call_count), (0, 0))
            self.assertEqual(before, [(self.root/n).read_bytes() for n in ('all-campaigns.md', 'all-campaigns.html')])
            (self.root/'all-campaigns.md').unlink()  # destructive-ok: own temporary fixture
            E.refresh(self.root)
            self.assertEqual((md.call_count, ht.call_count), (1, 1))

    def test_all_page_cache_invalidates_every_row_change(self):
        self.cycle("2026-01-02_one", "2026-01-02_alpha", [("artifacts/report.md", "# Hello\ntext")])
        snapshot = E.scan(self.root)
        self.assertTrue(snapshot['all_campaigns'])
        with mock.patch.object(E, 'scan', return_value=snapshot): E.refresh(self.root)
        for field, replacement in (('title', 'new title'), ('date', '2026-02-01'), ('cycles', 99), ('path', 'campaigns/other')):
            changed = copy.deepcopy(snapshot); changed['all_campaigns'][0][field] = replacement
            with mock.patch.object(E, 'scan', return_value=changed), mock.patch.object(E, 'render_all_html', wraps=E.render_all_html) as render:
                E.refresh(self.root)
                self.assertEqual(render.call_count, 1, field)
            with mock.patch.object(E, 'scan', return_value=snapshot): E.refresh(self.root)

    def test_open_live_missing_and_wrong_root(self):
        import artifact_viewer as V
        import threading
        self.cycle("2026-01-02_one", "2026-01-02_alpha", [("artifacts/report.md", "# Hello\ntext")])
        server = V.ViewerServer(self.root, 0)
        thread = threading.Thread(target=server.serve_forever); thread.start()
        try:
            real = V.viewer_url
            with mock.patch.object(V, 'viewer_url', side_effect=lambda root, target: real(root, target, server.server_address[1])):
                out = io.StringIO()
                with contextlib.redirect_stdout(out), mock.patch.object(E, '_open_externally') as opener:
                    self.assertEqual(E._command_open(self.root, ['Hello']), 0)
                lines = out.getvalue().splitlines()
                self.assertTrue(lines[0].endswith('report.md'))
                self.assertIn('http://127.0.0.1:', lines[1])
                self.assertTrue(opener.call_args.args[0].startswith('http://'))
                out = io.StringIO()
                with contextlib.redirect_stdout(out), mock.patch.object(E, '_open_externally') as opener:
                    self.assertEqual(E._command_open(self.root, ['1']), 0)  # the campaign folder line
                lines = out.getvalue().splitlines()
                self.assertTrue(lines[0].endswith('campaigns/2026-01-02_one'))
                self.assertRegex(lines[1], r'http://127\.0\.0\.1:\d+/campaigns/2026-01-02_one/$')
                self.assertIsNone(real(self.base, self.root/'index.html', server.server_address[1]))
            with mock.patch.dict(sys.modules, {'artifact_viewer': None}), contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(E._command_open(self.root, []), 0)
                self.assertEqual(out.getvalue().strip(), str(self.root/'index.html'))
        finally:
            server.shutdown(); thread.join(); server.server_close()


class EntrancePerformanceTests(EntranceFixture):
    def test_refresh_command_snapshot_benchmark(self):
        # A sealed A source baseline, copied only to this test's private scratch.
        for campaign in range(100):
            for cycle in range(10):
                self.cycle(f'2026-01-01_c{campaign:03}', f'2026-01-01_n{cycle:02}',
                           [('artifacts/report.md', '# Result\nshort report')])
        baseline_root = self.base/'baseline'; shutil.copytree(self.root, baseline_root)
        original = subprocess.check_output(['git', 'show', 'a1f81c142:utilities/artifact_entrance.py'], text=True)
        baseline_path = self.base/'baseline_entrance.py'; baseline_path.write_text(original)
        spec = importlib.util.spec_from_file_location('baseline_entrance', baseline_path)
        baseline = importlib.util.module_from_spec(spec); spec.loader.exec_module(baseline)
        baseline._translator = lambda: baseline._fallback_translator  # the old text keys are gone from the shared catalog
        baseline.ENTRANCE_NAMES = baseline.ENTRANCE_NAMES[:4]  # the old module knows four files only
        observations = []
        for name, module, root in [('baseline', baseline, baseline_root), ('current', E, self.root)]:
            timings = []
            with mock.patch.object(module, 'scan', wraps=module.scan) as scan, mock.patch.object(module, 'render_all_html', wraps=module.render_all_html) as render:
                start = time.perf_counter(); module.refresh(root); first = time.perf_counter()-start
                module.refresh(root)  # warm-up
                scans_before, renders_before = scan.call_count, render.call_count
                for _ in range(5):
                    start = time.perf_counter(); module.refresh(root); timings.append(time.perf_counter()-start)
                self.assertEqual(scan.call_count-scans_before, 5)
                if name == 'current': self.assertEqual(render.call_count-renders_before, 0)
                observations.append(f'{name} first={first:.4f}s warm median={statistics.median(timings):.4f}s min={min(timings):.4f}s max={max(timings):.4f}s scans={scan.call_count-scans_before} renders={render.call_count-renders_before}')
        print('\n100x10 warm-cache benchmark: ' + '; '.join(observations))

class SectionFixture(EntranceFixture):
    """A root with spec topics (older revision A, newer revision B), campaigns and route reports."""
    REF = "ref_" + "a" * 32
    REV_A, REV_B = "rrev_" + "1" * 32, "rrev_" + "2" * 32

    def spec(self, count=12, *, newest=("c01",), h1=None, top="# Contents — PRD\n\nIndex.\n"):
        import artifact_producer as P
        base = self.root / "shared" / "spec" / self.REF
        names = [f"c{number:02d}" for number in range(1, count + 1)]
        heads = h1 or (lambda name: f"# Component {name} — Spec (PRD)\n\nBody.\n")
        files = {f"{name}/prd.md": heads(name).encode() for name in names}
        files["prd.md"] = top.encode()
        newer = {f"{name}/prd.md" for name in newest} | {"prd.md"}
        for rid, day, physical in ((self.REV_A, "2026-11-10", set(files) - newer),
                                   (self.REV_B, "2026-11-18", newer)):
            directory = base / "revisions" / rid
            for name in physical:
                (directory / name).parent.mkdir(parents=True, exist_ok=True)
                (directory / name).write_bytes(files[name])
            directory.mkdir(parents=True, exist_ok=True)
            logical = files if rid == self.REV_B else {name: files[name] for name in physical}
            rows = P._spec_inventory(logical)
            record = {"schema_version": 1, "shared_reference_id": self.REF,
                      "shared_reference_revision_id": rid, "kind": "shared-spec", "files": rows,
                      "content_digest": P._spec_inventory_digest(rows), "created_on": day + "T09:00:00Z"}
            if rid == self.REV_B:
                record["stored_in"] = {name: self.REV_A for name in files if name not in newer}
            (directory / "revision.json").write_text(json.dumps(record))
        (base / "reference.json").write_text(json.dumps({
            "shared_reference_id": self.REF, "latest_revision_id": self.REV_B,
            "revisions": [self.REV_A, self.REV_B]}))
        return names

    def reports(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(E._command_reports(self.root), 0)
        return out.getvalue()

    def open_line(self, number, screen=True):
        out = io.StringIO()
        with mock.patch.object(E, "_show_viewer", return_value=None), \
                mock.patch.object(E, "_can_open_externally", return_value=screen), \
                mock.patch.object(E, "_open_externally"), contextlib.redirect_stdout(out):
            self.assertEqual(E._command_open(self.root, [str(number)]), 0)
        return out.getvalue().strip()

    def reports_targets(self):
        lines = self.reports().splitlines()
        return [lines[at + 1].strip() for at, line in enumerate(lines) if re.match(r"^\d+\. ", line)]


class SectionTests(SectionFixture):
    def _routes(self):
        self.cycle("2026-01-02_one", "2026-01-02_alpha",
                   [("artifacts/final_report.md", "# Alpha result\n\nFirst body.\n")])
        self.cycle("2026-01-03_two", "2026-01-03_beta",
                   [("artifacts/final_report.md", "# Beta result\n\nSecond body.\n")])

    def _written(self):
        return {name: self.entrance(name) for name in ("README.md", "index.html", "all-campaigns.md", "all-campaigns.html")}

    def test_a_campaign_line_opens_its_document_when_it_has_one_and_the_folder_otherwise(self):
        self._routes()
        self.refresh()
        before, terminal_before = self._written(), self.reports()
        self.assertIn("campaigns/2026-01-03_two", before["README.md"])
        self.assertNotIn("campaign.md", " ".join(before.values()) + terminal_before)
        self.assertEqual(self.open_line(1), str(self.root / "campaigns/2026-01-03_two"))
        page = self.root / "campaigns/2026-01-03_two/campaign.md"
        page.write_text("<!-- hearting:campaign-document v1 -->\n# Two\n\n**한 줄**: x\n", encoding="utf-8")
        self.refresh()
        after, terminal_after = self._written(), self.reports()
        for name, text in after.items():
            self.assertIn("campaigns/2026-01-03_two/campaign.md", text, name)
            # only the link target changed
            self.assertEqual(text.replace("campaigns/2026-01-03_two/campaign.md", "campaigns/2026-01-03_two"),
                             before[name], name)
        self.assertEqual(terminal_after.replace("campaigns/2026-01-03_two/campaign.md", "campaigns/2026-01-03_two"),
                         terminal_before)
        self.assertIn("campaigns/2026-01-03_two/campaign.md", terminal_after)
        self.assertEqual(self.open_line(1), str(page))
        # the other campaign, which has no document, still opens its folder
        self.assertEqual(self.open_line(2), str(self.root / "campaigns/2026-01-02_one"))

    def test_the_campaign_page_is_not_on_the_recent_route_reports_and_its_line_opens_it(self):
        self._routes()
        hand = self.root / "campaigns/2026-01-03_two/현황.md"
        hand.write_text("# Two status\n\n**한 줄**: hand written.\n", encoding="utf-8")
        page = self.root / "campaigns/2026-01-03_two/campaign.md"
        page.write_text("<!-- hearting:campaign-document v1 -->\n# Two\n\n**한 줄**: x\n", encoding="utf-8")
        self.refresh()
        for name in ("recent-route-reports.md", "recent-route-reports.html"):
            text = self.entrance(name)
            self.assertNotIn("campaign.md", text, name)
            self.assertNotIn("Two status", text, name)  # a hand-written status page beside it is not listed
        self.assertNotIn("campaign.md", " ".join(row["doc_path"] for row in self.rows()
                                                  if row.get("kind") == "campaign-document"))
        self.assertEqual(self.open_line(1), str(page))   # the campaign line opens it
        page.unlink()                                    # without a `campaign.md` the hand-written line stays
        self.refresh()
        for name in ("recent-route-reports.md", "recent-route-reports.html"):
            self.assertIn("Two status", self.entrance(name), name)

    def test_a_campaign_document_that_cannot_be_taken_leaves_the_folder(self):
        self._routes()
        page = self.root / "campaigns/2026-01-03_two/campaign.md"
        page.write_text("x" * 70000, encoding="utf-8")
        self.refresh()
        self.assertNotIn("campaign.md", self.entrance())
        page.unlink()
        outside = self.base / "outside.md"
        outside.write_text("# outside\n", encoding="utf-8")
        page.symlink_to(outside)
        self.refresh()
        self.assertNotIn("campaign.md", self.entrance())
        self.assertEqual(self.open_line(1), str(self.root / "campaigns/2026-01-03_two"))

    def test_the_all_campaigns_page_is_rewritten_when_a_document_appears(self):
        self._routes()
        self.refresh()
        first = self.entrance("all-campaigns.html")
        self.refresh()  # the second pass reuses what it wrote
        self.assertEqual(self.entrance("all-campaigns.html"), first)
        (self.root / "campaigns/2026-01-02_one/campaign.md").write_text("# One\n", encoding="utf-8")
        self.refresh()
        self.assertIn("campaigns/2026-01-02_one/campaign.md", self.entrance("all-campaigns.html"))
        self.assertIn("campaigns/2026-01-02_one/campaign.md", self.entrance("all-campaigns.md"))

    def test_the_top_has_spec_campaigns_and_records_and_no_route_reports(self):
        self.spec()
        self._routes()
        self.refresh()
        readme, page = self.entrance(), self.entrance("index.html")
        for text, marks in ((readme, ("## spec", "## Campaigns", "## Records")),
                            (page, ("<h2>spec</h2>", "<h2>Campaigns</h2>", "<h2>Records</h2>"))):
            positions = [text.index(mark) for mark in marks]
            self.assertEqual(positions, sorted(positions))
            for hidden in ("Alpha result", "Beta result", "final_report", "2026-01-02_alpha"):
                self.assertNotIn(hidden, text)
        self.assertIn("recent-route-reports.md", readme)
        self.assertIn('href="recent-route-reports.html"', page)
        for name in (E.RECORDS_MD, E.RECORDS_HTML):
            for shown in ("Alpha result", "Beta result", "2026-01-02_alpha"):
                self.assertIn(shown, self.entrance(name))

    def test_the_route_reports_page_keeps_the_rule_and_order_and_holds_no_spec_topic(self):
        self.spec()
        self._routes()
        self.cycle("2026-01-04_three", "2026-01-04_gamma",
                   [("artifacts/final_report.md", "# Gamma result\n\nThird body.\n")])
        self.refresh()
        rows = self.rows()
        self.assertEqual([row["title"] for row in rows], ["Gamma result", "Beta result", "Alpha result"])
        self.assertNotIn("canonical-topic", {row["kind"] for row in rows})
        page = self.entrance(E.RECORDS_MD)
        titles = re.findall(r"^- \S+  \[(.*?)\]\(<", page, re.MULTILINE)
        self.assertEqual(titles, [row["title"] for row in rows])
        self.assertNotRegex(page, r"(?m)^\d+\. ")  # the entrance's numbers belong to the entrance
        self.assertIn("<ul>", self.entrance(E.RECORDS_HTML))
        self.assertNotIn("<ol>", self.entrance(E.RECORDS_HTML))
        self.assertNotIn("Component", page)
        self.assertEqual(page, E.render_recent_markdown(E.scan(self.root), E._translator()))
        self.assertTrue(page.startswith(E.MARKER + "\n# Recent route reports\n"))
        self.assertIn("[Back to the entrance](<README.md>)", page)

    def _heads(self, name):
        return ("# 라우팅 — 갈래, 유닛, 엮기 — 제품 요구사항 (PRD)\n" if name == "c02"
                else "# Plain title\n" if name == "c03"
                else "No heading here\n" if name == "c04"
                else f"# Component {name} — Spec (PRD)\n")

    def test_spec_lines_dates_titles_order_limit_and_the_rest_line(self):
        self.spec(12, h1=self._heads)
        snapshot = E.scan(self.root)
        sections = E.entrance_sections(snapshot, E._translator())
        spec = sections[0]
        self.assertEqual(spec["key"], "spec")
        self.assertEqual(E.ENTRANCE_SPEC_LINES, 3)
        lines = spec["lines"]
        self.assertEqual(len(lines), E.ENTRANCE_SPEC_LINES + 1)
        self.assertEqual([line["date"] for line in lines[:3]], ["2026-11-18", "2026-11-10", "2026-11-10"])
        self.assertEqual([line["title"] for line in lines[:3]],
                         ["Component c01", "라우팅 — 갈래, 유닛, 엮기", "Plain title"])
        rest = lines[-1]
        self.assertEqual((rest["kind"], rest["title"]), ("spec-rest", "9 more"))
        self.assertEqual((rest["md"], rest["html"], rest["target"]), ("all-spec.md", "all-spec.html", "all-spec.html"))
        self.assertEqual(rest["number"], E.ENTRANCE_SPEC_LINES + 1)
        self.refresh()
        self.assertIn("4. [9 more](<all-spec.md>)", self.entrance())
        self.assertIn('<li><a href="all-spec.html">9 more</a></li>', self.entrance("index.html"))
        os.environ["FLEET_NOW_LANG"] = "ko"
        self.assertIn("4. [나머지 9개](<all-spec.md>)", E.render_markdown(E.scan(self.root), E._translator()))

    def test_the_rest_line_appears_only_when_a_topic_is_hidden(self):
        for count, hidden in ((2, None), (3, None), (4, 1), (5, 2)):
            self._clear_spec()
            self.spec(count)
            lines = E.entrance_sections(E.scan(self.root), E._translator())[0]["lines"]
            self.assertEqual(len(lines), min(count, 3) + (1 if hidden else 0), count)
            self.assertEqual(lines[-1]["kind"], "spec-rest" if hidden else "spec", count)
            if hidden:
                self.assertEqual(lines[-1]["title"], f"{hidden} more")

    def _clear_spec(self):
        shutil.rmtree(self.root / "shared", ignore_errors=True)  # destructive-ok: own temporary fixture

    def test_all_spec_page_holds_every_topic_in_the_entrance_order_and_form(self):
        self.spec(12, h1=self._heads)
        self.refresh()
        snapshot = E.scan(self.root)
        expected = [(row["date"], row["title"], E._record_md_href(row), E._record_href(row))
                    for row in snapshot["topics"] if row["role"] == "topic"]
        self.assertEqual(len(expected), 12)
        self.assertEqual([title for _, title, _, _ in expected][:4],
                         ["Component c01", "라우팅 — 갈래, 유닛, 엮기", "Plain title", "c04"])
        md = self.entrance(E.SPEC_MD)
        self.assertEqual(len([r for r in snapshot["topics"] if r["role"] == "contents"]), 1)
        self.assertEqual(re.findall(r"^- (\S+)  \[(.*?)\]\(<(.*?)>\)$", md, re.MULTILINE),
                         [(d, E._md_text(t), m) for d, t, m, _ in expected])
        page = self.entrance(E.SPEC_HTML)
        self.assertEqual(re.findall(r'<li><span class="meta">([^<]*)</span> <a href="([^"]+)">(.*?)</a></li>', page),
                         [(d, h, E._escape(t)) for d, t, _, h in expected])
        # the same line the entrance shows for each of its first three topics
        entrance = E.entrance_sections(snapshot, E._translator())[0]["lines"][:3]
        self.assertEqual([(l["date"], l["title"], l["md"], l["html"]) for l in entrance],
                         [(d, t, m, h) for d, t, m, h in expected[:3]])
        self.assertTrue(md.startswith(E.MARKER + "\n# All spec topics\n\n[Back to the entrance](<README.md>)\n"))
        self.assertIn("<h1>All spec topics</h1>", page)
        self.assertIn('<p><a href="index.html">Back to the entrance</a></p>', page)
        self.assertEqual(md, E.render_spec_markdown(snapshot, E._translator()))
        os.environ["FLEET_NOW_LANG"] = "ko"
        self.assertTrue(E.render_spec_markdown(E.scan(self.root), E._translator()).startswith(
            E.MARKER + "\n# spec 주제 전체\n"))

    def test_all_spec_page_with_no_topic_says_there_are_no_documents_like_the_route_reports_page(self):
        self._routes()
        self.refresh()
        for name in (E.SPEC_MD, E.SPEC_HTML, E.RECORDS_MD):
            self.assertTrue((self.root / name).is_file(), name)
        self.assertIn("No documents yet.", self.entrance(E.SPEC_MD))
        self.assertIn("<li>No documents yet.</li>", self.entrance(E.SPEC_HTML))
        empty = self.base / "empty"
        empty.mkdir()
        self.assertIn("No documents yet.", E.render_recent_markdown(E.scan(empty), E._translator()))
        self.assertIn("No documents yet.", E.render_spec_markdown(E.scan(empty), E._translator()))

    def test_all_spec_page_is_deterministic_recreated_and_follows_the_ownership_rule(self):
        self.spec(12)
        self.refresh()
        before = {name: (self.root / name).read_bytes() for name in (E.SPEC_MD, E.SPEC_HTML)}
        self.assertEqual(self.refresh()["status"], "unchanged")
        (self.root / E.SPEC_HTML).unlink()  # destructive-ok: own temporary fixture
        result = self.refresh()
        self.assertEqual((result["status"], result["created"]), ("written", [E.SPEC_HTML]))
        self.assertEqual({name: (self.root / name).read_bytes() for name in before}, before)
        self.assertEqual(len(E.ENTRANCE_NAMES), 8)
        self.assertIn(E.SPEC_MD, E.ENTRANCE_NAMES)
        self.assertIn(E.SPEC_HTML, E.ENTRANCE_NAMES)
        for name in (E.SPEC_MD, E.SPEC_HTML):
            self.assertTrue(_has_entrance_marker(self.root / name), name)
        # a same-named file without the marker, and a link, are neither overwritten nor followed
        mine = "# my own spec page\n"
        (self.root / E.SPEC_MD).write_text(mine, encoding="utf-8")
        outside = self.base / "outside-spec.html"
        outside.write_text("outside\n", encoding="utf-8")
        (self.root / E.SPEC_HTML).unlink()  # destructive-ok: own temporary fixture
        os.symlink(outside, self.root / E.SPEC_HTML)
        result = self.refresh()
        self.assertEqual(sorted(result["foreign"]), sorted([E.SPEC_MD, E.SPEC_HTML]))
        self.assertEqual((self.root / E.SPEC_MD).read_text(encoding="utf-8"), mine)
        self.assertTrue((self.root / E.SPEC_HTML).is_symlink())
        self.assertEqual(outside.read_text(encoding="utf-8"), "outside\n")
        self.assertEqual(self.refresh()["status"], "unchanged")

    def test_section_notes_are_on_markdown_html_and_the_terminal_and_are_not_numbered(self):
        self.spec(12)
        self._routes()
        self.refresh()
        notes = ("How this repo works now. 12 topics; each change edits the same document.",
                 "One document per stream of work. No new document appears; this one is rewritten.", "Reports left by routes. Not usually opened.")
        readme, page, terminal = self.entrance(), self.entrance("index.html"), self.reports()
        self.assertIn("## spec\n\n" + notes[0] + "\n\n1. ", readme)
        self.assertIn("## Campaigns\n\n" + notes[1] + "\n\n5. ", readme)
        self.assertIn("8. [Recent route reports](<recent-route-reports.md>)\n   " + notes[2], readme)
        self.assertIn(f'<h2>spec</h2>\n<p class="meta">{notes[0]}</p>\n<ul>', page)
        self.assertIn(f'<h2>Campaigns</h2>\n<p class="meta">{notes[1]}</p>\n<ul>', page)
        self.assertIn(f'<a href="recent-route-reports.html">Recent route reports</a> <span class="meta">{notes[2]}</span></li>', page)
        self.assertIn(f"spec\n   {notes[0]}\n1. ", terminal)
        self.assertIn(f"Campaigns\n   {notes[1]}\n5. ", terminal)
        self.assertIn(f"8. Recent route reports\n   recent-route-reports.md\n   {notes[2]}\n", terminal)
        for text in (readme, terminal):
            self.assertNotRegex(text, r"(?m)^\d+\. (How this|One line|Reports left)")
        os.environ["FLEET_NOW_LANG"] = "ko"
        self.refresh()
        readme = self.entrance()
        for note in ("이 repo가 지금 어떻게 동작하는지. 주제 12개, 고칠 때마다 같은 문서를 고칩니다.",
                     "일의 흐름 하나에 문서 한 장. 새 문서가 생기지 않고 이 문서가 고쳐집니다.", "route가 남긴 보고입니다. 평소에는 열지 않습니다."):
            self.assertIn(note, readme)
        self.assertIn("[전체 캠페인 2개](<all-campaigns.md>)", readme)

    def test_the_entrance_has_the_agreed_shape(self):
        self.spec(18)
        self._routes()
        self.refresh()
        readme = self.entrance()
        lines = readme.splitlines()
        sections = [at for at, line in enumerate(lines) if line.startswith("## ")]
        self.assertEqual([lines[at] for at in sections], ["## spec", "## Campaigns", "## Records"])
        numbers = [int(m.group(1)) for line in lines if (m := re.match(r"^(\d+)\. ", line))]
        self.assertEqual(numbers, list(range(1, 10)))
        spec_part = "\n".join(lines[sections[0]:sections[1]])
        self.assertEqual(len(re.findall(r"(?m)^\d+\. ", spec_part)), E.ENTRANCE_SPEC_LINES + 1)
        self.assertIn("[15 more](<all-spec.md>)", spec_part)
        campaign_part = "\n".join(lines[sections[1]:sections[2]])
        self.assertEqual(len(re.findall(r"(?m)^\d+\. ", campaign_part)), 2 + 1)  # two campaigns and the fold line

    def test_no_components_means_no_spec_section(self):
        self._routes()
        self.refresh()
        self.assertNotIn("## spec", self.entrance())
        self.assertNotIn("<h2>spec</h2>", self.entrance("index.html"))
        self.assertEqual([section["key"] for section in
                          E.entrance_sections(E.scan(self.root), E._translator())], ["campaigns", "records"])

    def test_campaign_lines_carry_date_title_and_the_meta_summary(self):
        self._routes()
        by_id = {"camp_" + _digest("campaign:2026-01-02_one"): {"title": "First campaign", "summary": "  one   line  "},
                 "camp_" + _digest("campaign:2026-01-03_two"): {"title": "Second campaign"}}
        for name in ("2026-01-02_one", "2026-01-03_two"):
            (self.root / "campaigns" / name / "meta.json").write_text("{}")
        def read(root, campaign_id, members=None):
            import types
            return types.SimpleNamespace(status="ok", doc={"campaign": by_id[campaign_id]})
        with mock.patch.object(E.artifact_meta, "read_campaign_meta", side_effect=read), \
                mock.patch.object(E.artifact_meta, "_membership", return_value={}):
            self.refresh()
            snapshot = E.scan(self.root)
        self.assertEqual([(row["title"], row["summary"]) for row in snapshot["campaigns"]],
                         [("Second campaign", ""), ("First campaign", "one line")])
        for gone in ("docs", "doc_cycles", "records"):
            self.assertNotIn(gone, snapshot["campaigns"][0])
        readme = self.entrance()
        self.assertIn("1. 2026-01-03  [Second campaign](<campaigns/2026-01-03_two>)\n2. 2026-01-02  "
                      "[First campaign](<campaigns/2026-01-02_one>)\n   one line\n3. [All 2 campaigns]", readme)
        self.assertIn('<li><span class="meta">2026-01-02</span> <a href="campaigns/2026-01-02_one">First '
                      'campaign</a><br><span class="meta">one line</span></li>', self.entrance("index.html"))
        self.assertNotIn("Alpha result", readme + self.entrance("index.html"))

    def test_the_entrance_reports_and_open_number_name_the_same_lines(self):
        names = self.spec(12)
        self._routes()
        self.refresh()
        html = re.findall(r'<li>(?:<span class="meta">[^<]*</span> )?<a href="([^"]+)">', self.entrance("index.html"))
        targets = self.reports_targets()
        # the terminal names a derived chapter by its `.md`; index.html and `open` use the `.html`
        shown = lambda t: t[:-3] + ".html" if t in ("all-spec.md", "all-campaigns.md", E.RECORDS_MD) else t
        self.assertEqual(html, [__import__("urllib.parse").parse.quote(shown(t), safe="/.") for t in targets])
        md = re.findall(r"^(\d+)\. (?:\S+  )?\[(.*?)\]\(<(.*?)>\)", self.entrance(), re.MULTILINE)
        self.assertEqual([int(number) for number, _, _ in md], list(range(1, len(targets) + 1)))
        self.assertEqual(len(md), len(targets))
        self.assertEqual(targets[-3:], ["all-campaigns.md", "recent-route-reports.md", "campaigns/INDEX.md"])
        for number, target in enumerate(targets, start=1):
            expected = self.root / shown(target)
            self.assertEqual(self.open_line(number), str(expected), number)
        self.assertTrue((self.root / targets[-4]).is_dir(), targets[-4])  # the second campaign's folder
        spec_first = self.open_line(1)
        self.assertTrue(spec_first.endswith("c01/prd.md"))
        self.assertEqual(targets[E.ENTRANCE_SPEC_LINES], "all-spec.md")
        self.assertEqual(self.open_line(E.ENTRANCE_SPEC_LINES + 1), str(self.root / "all-spec.html"))
        campaign_line = E.ENTRANCE_SPEC_LINES + 2
        self.assertEqual(self.open_line(campaign_line), str(self.root / "campaigns/2026-01-03_two"))
        self.assertEqual(names[0], "c01")

    def test_open_words_find_route_reports_and_spec_topics(self):
        self.spec(12)
        self._routes()
        self.refresh()
        out = io.StringIO()
        with mock.patch.object(E, "_show_viewer", return_value=None), mock.patch.object(E, "_open_externally"), \
                contextlib.redirect_stdout(out):
            self.assertEqual(E._command_open(self.root, ["Alpha", "result"]), 0)
            self.assertEqual(E._command_open(self.root, ["c11"]), 0)
        first, second = out.getvalue().splitlines()
        self.assertTrue(first.endswith("2026-01-02_alpha/artifacts/final_report.md"))
        self.assertIn("/shared/spec/", second)
        self.assertTrue(second.endswith("c11/prd.md"))

    def test_the_route_reports_page_is_deterministic_recreated_and_never_cached(self):
        self._routes()
        self.refresh()
        before = {name: (self.root / name).read_bytes() for name in (E.RECORDS_MD, E.RECORDS_HTML)}
        self.assertEqual(self.refresh()["status"], "unchanged")
        (self.root / E.RECORDS_MD).unlink()  # destructive-ok: own temporary fixture
        result = self.refresh()
        self.assertEqual((result["status"], result["created"]), ("written", [E.RECORDS_MD]))
        self.assertEqual((self.root / E.RECORDS_MD).read_bytes(), before[E.RECORDS_MD])
        self.cycle("2026-01-04_three", "2026-01-04_gamma",
                   [("artifacts/final_report.md", "# Gamma result\n\nThird body.\n")])
        self.refresh()
        self.assertIn("Gamma result", self.entrance(E.RECORDS_MD))
        self.assertNotEqual((self.root / E.RECORDS_HTML).read_bytes(), before[E.RECORDS_HTML])

    def test_a_route_reports_page_without_the_marker_or_a_link_is_never_overwritten_or_followed(self):
        self._routes()
        mine = "# my own page\n"
        (self.root / E.RECORDS_MD).write_text(mine, encoding="utf-8")
        outside = self.base / "outside.html"
        outside.write_text("outside\n", encoding="utf-8")
        os.symlink(outside, self.root / E.RECORDS_HTML)
        result = self.refresh()
        self.assertEqual(sorted(result["foreign"]), sorted([E.RECORDS_MD, E.RECORDS_HTML]))
        self.assertEqual((self.root / E.RECORDS_MD).read_text(encoding="utf-8"), mine)
        self.assertTrue((self.root / E.RECORDS_HTML).is_symlink())
        self.assertEqual(outside.read_text(encoding="utf-8"), "outside\n")
        for name in ("README.md", "index.html", "all-campaigns.md", "all-campaigns.html"):
            self.assertTrue(_has_entrance_marker(self.root / name), name)
        self.assertEqual(self.refresh()["status"], "unchanged")

    def test_no_screen_text_uses_cycle_or_user_facing_in_either_language(self):
        self.spec(12)
        self._routes()
        self.cycle("2026-01-05_three", "2026-01-05_open-one", [])
        for lang in ("en", "ko"):
            os.environ["FLEET_NOW_LANG"] = lang
            self.refresh()
            shown = [self.entrance(name) for name in E.ENTRANCE_NAMES] + [self.reports()]
            self.assertEqual(len(shown), 9)
            for text in shown:
                lowered = text.lower()
                for word in ("사이클", "cycle", "user-facing"):
                    self.assertNotIn(word, lowered, lang)
        self.assertIn("route", self.entrance())

    def test_the_viewer_serves_the_route_reports_page_like_any_derived_file(self):
        import http.client
        import artifact_viewer as V
        self._routes()
        self.refresh()
        server = V.ViewerServer(self.root, 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            for name in (E.RECORDS_MD, E.RECORDS_HTML):
                conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
                conn.request("GET", "/" + name)
                response = conn.getresponse()
                body = response.read()
                conn.close()
                self.assertEqual(response.status, 200, name)
                self.assertIn(b"Alpha result", body, name)
            conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
            conn.request("GET", "/" + E.RECORDS_HTML + "?raw=1")
            response = conn.getresponse()
            raw = response.read()
            conn.close()
            self.assertEqual(response.status, 200)
            self.assertEqual(raw, (self.root / E.RECORDS_HTML).read_bytes())
        finally:
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()

    # --- review fixes (PR #1746): top prd.md, contents line, open range, opens, order, empty state ---

    def test_a_reference_with_only_a_top_prd_is_a_topic_everywhere_it_is_listed(self):
        self.spec(0, newest=(), top="# Project — PRD\n\nBody.\n")
        self._routes()
        self.refresh()
        topics = E.scan(self.root)["topics"]
        self.assertEqual([(row["role"], row["title"], row["date"]) for row in topics],
                         [("topic", "Project", "2026-11-18")])
        spec = E.entrance_sections(E.scan(self.root), E._translator())[0]
        self.assertEqual(spec["key"], "spec")
        self.assertEqual([(line["kind"], line["title"]) for line in spec["lines"]], [("spec", "Project")])
        self.assertIn("1 topics", spec["note"])
        prd = f"shared/spec/{self.REF}/revisions/{self.REV_B}/prd.md"
        self.assertIn(f"1. 2026-11-18  [Project](<{prd}>)", self.entrance())
        self.assertIn(f'<a href="{prd}">Project</a>', self.entrance("index.html"))
        self.assertIn(f"- 2026-11-18  [Project](<{prd}>)", self.entrance(E.SPEC_MD))
        self.assertIn(f'<li><span class="meta">2026-11-18</span> <a href="{prd}">Project</a></li>',
                      self.entrance(E.SPEC_HTML))
        self.assertNotIn("Contents", self.entrance(E.SPEC_MD))
        self.assertEqual(self.open_line(1), str(self.root / prd))
        self.assertIn("1. 2026-11-18  Project\n   " + prd, self.reports())

    def test_a_top_only_reference_without_a_heading_is_titled_by_the_reference(self):
        self.spec(0, newest=(), top="No heading here\n")
        topic = E.scan(self.root)["topics"][0]
        self.assertEqual((topic["role"], topic["title"]), ("topic", self.REF))

    def test_the_contents_prd_of_a_component_reference_is_one_line_on_all_spec_and_no_topic(self):
        self.spec(12)
        self._routes()
        self.refresh()
        snapshot = E.scan(self.root)
        roles = [row["role"] for row in snapshot["topics"]]
        self.assertEqual((roles.count("topic"), roles.count("contents")), (12, 1))
        top = f"shared/spec/{self.REF}/revisions/{self.REV_B}/prd.md"
        md = self.entrance(E.SPEC_MD)
        head = "\n".join(md.splitlines()[:7])
        self.assertTrue(md.startswith(E.MARKER + "\n# All spec topics\n\n[Back to the entrance](<README.md>)\n\n"
                                      f"2026-11-18  [Contents — PRD](<{top}>)\n\n- 2026-11-18  [Component c01]"), head)
        self.assertEqual(md.count(top), 1)
        html = self.entrance(E.SPEC_HTML)
        self.assertIn('<p><a href="index.html">Back to the entrance</a></p>\n<p><span class="meta">2026-11-18</span> '
                      f'<a href="{top}">Contents — PRD</a></p>\n<ul>', html)
        for text in (self.entrance(), self.entrance("index.html"), self.entrance(E.RECORDS_MD),
                     self.entrance(E.RECORDS_HTML), self.reports()):
            self.assertNotIn(top, text)
            self.assertNotIn("Contents", text)
        self.assertIn("12 topics", self.entrance())
        self.assertIn("[9 more](<all-spec.md>)", self.entrance())  # 12 topics, 3 shown; the contents line is not counted
        self.assertEqual(len(re.findall(r"(?m)^- ", md)), 12)

    def test_a_contents_prd_without_a_heading_reads_contents_or_the_korean_word(self):
        self.spec(4, top="No heading here\n")
        self.assertIn("[Contents](<", self.entrance_after_refresh(E.SPEC_MD))
        os.environ["FLEET_NOW_LANG"] = "ko"
        self.refresh()
        self.assertIn("[목차](<", self.entrance(E.SPEC_MD))
        self.assertIn(">목차</a></p>", self.entrance(E.SPEC_HTML))

    def entrance_after_refresh(self, name):
        self.refresh()
        return self.entrance(name)

    def test_a_number_outside_the_entrance_never_becomes_a_search(self):
        self.spec(12)
        self._routes()
        self.cycle("2026-01-12_four", "2026-01-12_delta",
                   [("artifacts/final_report.md", "# Delta result\n\nFourth body.\n")])
        self.refresh()
        lines = [item for section in E.entrance_sections(E.scan(self.root), E._translator())
                 for item in section["lines"] if item["number"] is not None]
        self.assertEqual(len(lines), 10)
        for number in ("0", "11", "12", "2026"):
            err = io.StringIO()
            with mock.patch.object(E, "_show_viewer") as viewer, mock.patch.object(E, "_open_externally") as opener, \
                    contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(err):
                code = E._command_open(self.root, [number])
            self.assertEqual(code, 1, number)
            self.assertEqual(out.getvalue(), "", number)
            self.assertEqual(err.getvalue().strip(),
                             f"There is no line {number}. The entrance has lines 1 to 10.", number)
            viewer.assert_not_called()
            opener.assert_not_called()
        os.environ["FLEET_NOW_LANG"] = "ko"
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            self.assertEqual(E._command_open(self.root, ["12"]), 1)
        self.assertEqual(err.getvalue().strip(), "12번 줄이 없습니다. 지금 입구는 1번부터 10번까지입니다.")
        os.environ.pop("FLEET_NOW_LANG")
        # words, alone or mixed with a number, still search
        with mock.patch.object(E, "_show_viewer", return_value=None), mock.patch.object(E, "_open_externally"), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(E._command_open(self.root, ["delta"]), 0)
            self.assertEqual(E._command_open(self.root, ["2026-01-12", "delta"]), 0)
        self.assertEqual(len(out.getvalue().splitlines()), 2)
        self.assertTrue(out.getvalue().splitlines()[0].endswith("2026-01-12_delta/artifacts/final_report.md"))
        self.assertEqual(self.open_line(10), str(self.root / "campaigns/INDEX.md"))

    def test_a_topic_is_not_opened_a_second_time_for_its_title_or_its_date(self):
        self.spec(12)
        self.refresh()
        with mock.patch.object(E, "_read_head", side_effect=AssertionError("titles come from the verified bytes")), \
                mock.patch("artifact_spec_storage.revision_created_on",
                           side_effect=AssertionError("dates come from the headers already read")):
            snapshot = E.scan(self.root)
        self.assertEqual([row["date"] for row in snapshot["topics"][:3]], ["2026-11-18", "2026-11-18", "2026-11-10"])
        self.assertEqual(snapshot["topics"][1]["title"], "Component c01")
        seen, live = [], [True]  # an audit hook cannot be removed: it goes quiet once `live` is cleared
        def hook(event, args):
            if live[0] and event == "open" and isinstance(args[0], str):
                seen.append(os.path.basename(args[0]))  # the no-follow reader opens one path part at a time
        sys.addaudithook(hook)
        try:
            E.scan(self.root)
            at_twelve = (seen.count("prd.md"), seen.count("revision.json"))
            live[0] = False  # the fixture's own writes are not reads
            seen.clear()
            self._clear_spec()
            self.spec(4)
            live[0] = True
            E.scan(self.root)
            at_four = (seen.count("prd.md"), seen.count("revision.json"))
        finally:
            live[0] = False
        self.assertEqual(at_twelve[0], 13)  # every PRD, the contents one included, once
        self.assertEqual(at_four[0], 5)
        self.assertEqual(at_twelve[1], at_four[1])  # header opens do not grow with the topic count

    def test_a_failed_write_never_leaves_the_entrance_pointing_at_a_missing_chapter(self):
        self.spec(12)
        self._routes()
        real, calls = E.store._write_atomic, []
        def fail_on_fifth(path, data, *args, **kwargs):
            calls.append(Path(path).name)
            if len(calls) == 5:
                raise OSError(28, "No space left on device")
            return real(path, data, *args, **kwargs)
        with mock.patch.object(E.store, "_write_atomic", side_effect=fail_on_fifth), \
                self.assertRaises(OSError):
            self.refresh()
        self.assertEqual(len(calls), 5)
        self.assertEqual(calls[:4], ["all-campaigns.md", "all-campaigns.html", E.RECORDS_MD, E.RECORDS_HTML])
        for name in ("README.md", "index.html"):
            self.assertFalse((self.root / name).exists(), name)
        # whichever files do exist only link to files that exist
        self.assertEqual(self.refresh()["status"], "written")
        order = []
        with mock.patch.object(E.store, "_write_atomic", side_effect=lambda path, *a, **k: order.append(Path(path).name)):
            self.cycle("2026-01-09_nine", "2026-01-09_iota", [("artifacts/final_report.md", "# Iota\n")])
            self.refresh()
        self.assertEqual(order[-2:], ["README.md", "index.html"])

    def test_the_terminal_prints_the_campaign_summary_and_names_derived_chapters_by_md(self):
        self.spec(12)
        self._routes()
        for name in ("2026-01-02_one", "2026-01-03_two"):
            (self.root / "campaigns" / name / "meta.json").write_text("{}")
        by_id = {"camp_" + _digest("campaign:2026-01-02_one"): {"title": "First", "summary": "first line"},
                 "camp_" + _digest("campaign:2026-01-03_two"): {"title": "Second"}}
        read = lambda root, cid, members=None: __import__("types").SimpleNamespace(status="ok", doc={"campaign": by_id[cid]})
        with mock.patch.object(E.artifact_meta, "read_campaign_meta", side_effect=read), \
                mock.patch.object(E.artifact_meta, "_membership", return_value={}):
            terminal = self.reports()
        self.assertIn("6. 2026-01-02  First\n   campaigns/2026-01-02_one\n   first line\n7. All 2 campaigns\n"
                      "   all-campaigns.md\n", terminal)
        self.assertIn("4. 9 more\n   all-spec.md\n", terminal)
        self.assertIn("8. Recent route reports\n   recent-route-reports.md\n", terminal)
        self.assertNotIn(".html", terminal)  # index.html's links stay `.html`; the terminal reads `.md`
        self.assertIn('href="all-spec.html"', self.entrance("index.html"))

    def test_an_empty_root_says_no_documents_once_and_has_no_all_campaigns_line(self):
        terminal = self.reports()
        self.assertEqual(terminal.count("No documents yet."), 1, terminal)
        self.assertNotIn("0 campaigns", terminal)
        self.assertNotRegex(terminal, r"(?m)^\d+\. All ")
        self.assertEqual(terminal.splitlines()[-1], f"Entrance: {self.root / 'README.md'}")
        lines = [item for section in E.entrance_sections(E.scan(self.root), E._translator())
                 for item in section["lines"] if item["number"] is not None]
        self.assertEqual([item["kind"] for item in lines], ["recent-routes", "id-index"])
        self.assertEqual([item["number"] for item in lines], [1, 2])
        self.assertNotIn("campaigns](<all-campaigns", self.entrance_after_refresh("README.md"))
        self.assertIn("1. [Recent route reports](<recent-route-reports.md>)", self.entrance())

    def test_markdown_summary_lines_show_angle_brackets_and_ampersands_as_typed(self):
        self._routes()
        (self.root / "campaigns" / "2026-01-02_one" / "meta.json").write_text("{}")
        cid = "camp_" + _digest("campaign:2026-01-02_one")
        doc = {"campaign": {"title": "A <b>T</b> & co", "summary": "x <i>tag</i> & *star* a>b"}}
        read = lambda root, campaign_id, members=None: __import__("types").SimpleNamespace(
            status="ok", doc=doc if campaign_id == cid else {"campaign": {"title": "Other"}})
        with mock.patch.object(E.artifact_meta, "read_campaign_meta", side_effect=read), \
                mock.patch.object(E.artifact_meta, "_membership", return_value={}):
            self.refresh()
        readme = self.entrance()
        self.assertIn("   x \\<i\\>tag\\</i\\> \\& \\*star\\* a\\>b\n", readme)
        self.assertNotIn("<i>", readme)
        self.assertNotIn("&amp;", readme)  # backslash escapes, never entities: the viewer does not decode them
        self.assertIn("[A <b>T</b> & co]", readme)  # the title keeps its earlier behaviour
        escaped = E._md_summary(doc["campaign"]["summary"])
        self.assertEqual(escaped, "x \\<i\\>tag\\</i\\> \\& \\*star\\* a\\>b")
        import artifact_markdown
        # the viewer shows the summary as it was typed (its HTML escapes the three characters once)
        self.assertEqual(artifact_markdown.render_markdown(escaped + "\n").strip(),
                         "<p>x &lt;i&gt;tag&lt;/i&gt; &amp; *star* a&gt;b</p>")
        self.assertIn("x &lt;i&gt;tag&lt;/i&gt; &amp; *star* a&gt;b", self.entrance("index.html"))

    def test_a_shell_without_a_screen_opens_the_md_chapter_and_a_screen_opens_the_html_one(self):
        self.spec(12)
        self._routes()
        self.refresh()
        lines = [item for section in E.entrance_sections(E.scan(self.root), E._translator())
                 for item in section["lines"] if item["number"] is not None]
        derived = [item for item in lines if item["kind"] in E.DERIVED_KINDS]
        self.assertEqual({item["kind"] for item in derived}, set(E.DERIVED_KINDS))
        for item in derived:
            self.assertTrue(item["md"].endswith(".md") and item["target"].endswith(".html"), item)
            self.assertEqual(self.open_line(item["number"], screen=False), str(self.root / item["md"]), item["kind"])
            self.assertEqual(self.open_line(item["number"], screen=True), str(self.root / item["target"]), item["kind"])
        other = next(item for item in lines if item["kind"] not in E.DERIVED_KINDS)
        self.assertEqual(self.open_line(other["number"], screen=False), self.open_line(other["number"], screen=True))

    def test_a_number_with_no_entrance_lines_says_not_found_and_names_no_range(self):
        missing = self.root / "no-such-root"
        err = io.StringIO()
        with mock.patch.object(E, "_show_viewer") as viewer, mock.patch.object(E, "_open_externally") as opener, \
                contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(err):
            code = E._command_open(missing, ["1"])
        self.assertEqual(code, 1)
        self.assertEqual(out.getvalue(), "")
        self.assertEqual(err.getvalue().strip(), "No artifact matches 1.")
        self.assertNotIn("lines 1 to 0", err.getvalue())
        viewer.assert_not_called()
        opener.assert_not_called()

    def test_the_route_reports_page_has_no_numbers_in_either_form_and_keeps_its_order_and_limit(self):
        for day in range(1, 23):
            self.cycle(f"2026-02-{day:02d}_c", f"2026-02-{day:02d}_r",
                       [("artifacts/final_report.md", f"# Report {day}\n\nBody.\n")])
        self.refresh()
        md, html = self.entrance(E.RECORDS_MD), self.entrance(E.RECORDS_HTML)
        titles = re.findall(r"(?m)^- \S+  \[(Report \d+)\]", md)
        self.assertEqual(titles, [f"Report {day}" for day in range(22, 2, -1)])
        self.assertNotRegex(md, r"(?m)^\d+\. ")
        self.assertEqual(html.count("<li>"), 20)
        self.assertNotIn("<ol>", html)


BACKLOG = """# Backlog
갱신 2026-11-21 23:10

## 2026-01-01_some-campaign
- First job
  date: 2026-11-20
  source: a talk
  note: said then
  - not a job: a sub-bullet
- Second job

```
- inside a code block
```

~~~text
- inside a tilde block
~~~

## Unassigned
- Third job
"""


class BacklogTests(SectionFixture):
    def _setup(self, text=BACKLOG):
        self.spec(5)
        self._routes()
        (self.root / "backlog.md").write_text(text, encoding="utf-8")
        self.refresh()

    def _routes(self):
        self.cycle("2026-01-02_one", "2026-01-02_alpha",
                   [("artifacts/final_report.md", "# Alpha result\n\nFirst body.\n")])
        self.cycle("2026-01-03_two", "2026-01-03_beta",
                   [("artifacts/final_report.md", "# Beta result\n\nSecond body.\n")])

    def _headings(self, readme):
        return [line for line in readme.splitlines() if line.startswith("## ")]

    def test_the_backlog_section_stands_between_the_campaigns_and_the_records_in_both_files(self):
        self._setup()
        readme, page = self.entrance(), self.entrance("index.html")
        self.assertEqual(self._headings(readme), ["## spec", "## Campaigns", "## Backlog", "## Records"])
        marks = ("<h2>spec</h2>", "<h2>Campaigns</h2>", "<h2>Backlog</h2>", "<h2>Records</h2>")
        positions = [page.index(mark) for mark in marks]
        self.assertEqual(positions, sorted(positions))
        self.assertIn("The session that postpones work writes one line each.", readme)
        self.assertIn("[Open backlog.md](<backlog.md>)\n   FIX ? · IMPR ? · NOTE ? · TBD 3 · 3 total · updated 2026-11-21 23:10", readme)
        self.assertIn('<a href="backlog.md">Open backlog.md</a> '
                      '<span class="meta">FIX ? · IMPR ? · NOTE ? · TBD 3 · 3 total · updated 2026-11-21 23:10</span>', page)
        self.assertEqual(readme.count("(<backlog.md>)"), 1)
        self.assertEqual(page.count('href="backlog.md"'), 1)

    def test_only_a_top_level_bullet_outside_a_code_block_is_a_job(self):
        self._setup()
        self.assertIn("3 total", self.entrance())
        self._setup("# B\n## C\n- one\n  - nested\n    - deeper\n\n````\n```\n- fenced\n````\n- two\n")
        self.assertIn("2 total", self.entrance())
        self.assertNotIn("updated", self.entrance())  # no 갱신 line, no date
        self._setup("# B\n")
        self.assertIn("0 total", self.entrance())

    def _count(self, text):
        (self.root / "backlog.md").write_text(text, encoding="utf-8")
        return E._backlog(self.root)["count"]

    def test_a_job_is_the_top_level_dash_bullet_under_a_heading(self):
        self.spec(5)
        self.assertEqual(self._count("# B\n- before\n- any heading\n## C\n- one\n- two\n"), 2)
        self.assertEqual(self._count("# B\n- a\n- b\n"), 0)
        self.assertEqual(self._count("## C\n* star\n+ plus\n1. numbered\n2) numbered\n-tight\n- one\n"), 1)
        self.assertEqual(self._count("## C\n  - indented\n\t- tabbed\n- one\n    - deeper\n"), 1)
        self.assertEqual(self._count("## A\n- one\n### sub\n- two\n## B\n- three\n"), 3)

    def test_a_fence_closes_only_with_the_same_character_at_least_as_long(self):
        self.spec(5)
        self.assertEqual(self._count("## C\n````\n```\n- fenced\n````\n- one\n"), 1)      # shorter does not close
        self.assertEqual(self._count("## C\n```\n~~~\n- fenced\n```\n- one\n"), 1)       # another character does not close
        self.assertEqual(self._count("## C\n```\n- fenced\n`````\n- one\n"), 1)          # longer closes
        self.assertEqual(self._count("## C\n~~~\n- fenced\n~~~\n- one\n- two\n"), 2)
        self.assertEqual(self._count("## C\n```\n- never closed\n- still fenced\n"), 0)    # an open fence runs to the end
        self.assertEqual(self._count("## C\n    ```\n- one\n"), 1)                        # four spaces is no fence

    def test_a_heading_may_be_indented_up_to_three_spaces(self):
        self.spec(5)
        self.assertEqual(self._count("# B\n ## C\n- a\n"), 1)
        self.assertEqual(self._count("# B\n   ## C\n- a\n"), 1)
        self.assertEqual(self._count("# B\n   ##\n- a\n"), 1)
        self.assertEqual(self._count("# B\n    ## C\n- a\n"), 0)       # four spaces is no heading
        self.assertEqual(self._count("# B\n\t## C\n- a\n"), 0)
        self.assertEqual(self._count("# B\n ### C\n- a\n"), 0)
        self.assertEqual(self._count("# B\n ## C\n - indented\n- a\n"), 1)  # the job itself stays at the left edge

    def test_the_update_line_ends_at_an_indented_heading_but_not_at_a_four_space_one(self):
        self.spec(5)
        def updated(text):
            (self.root / "backlog.md").write_text(text, encoding="utf-8")
            return E._backlog(self.root)["updated"]
        self.assertEqual(updated("# B\n ## C\n갱신 2026-11-21 23:10\n- a\n"), "")
        self.assertEqual(updated("# B\n    ## C\n갱신 2026-11-21 23:10\n## D\n"), "2026-11-21 23:10")

    def test_the_counting_rule_is_by_the_shape_of_the_line_not_by_markdown(self):
        self.spec(5)
        self.assertEqual(self._count("## C\n- - -\n"), 1)                             # a rule in Markdown, a job here
        self.assertEqual(self._count("## C\n<!--\n- hidden\n-->\n- one\n"), 2)     # a comment is not read
        self.assertEqual(self._count("## C\n```\n## D\n```\n- one\n"), 1)           # a fenced heading is no heading
        self.assertEqual(self._count("```\n## D\n```\n- one\n"), 0)                 # so no heading opens the jobs
        self.assertEqual(self._count("## C\n```\n- f\n``` x\n- g\n"), 0)            # text after a closer: still fenced
        self.assertEqual(self._count("## C\n```\n- f\n```  \n- one\n"), 1)          # trailing spaces still close
        self.assertEqual(self._count("## C\n```\n- f\n   ```\n- one\n"), 1)         # up to three spaces before a closer
        self.assertEqual(self._count("## C\n> - quoted\n-\n- one\n"), 1)

    def test_the_counting_rule_is_written_in_the_backlog_section_of_the_meta_contract(self):
        meta = (HERE.parent / "core" / "ARTIFACT_META.md").read_text(encoding="utf-8")
        section = meta[meta.index("### `backlog.md`"):]
        self.assertIn("A job is a line whose first two characters are `- `,", section)
        self.assertIn("at most three\n    spaces", section)
        self.assertIn("`- - -`, and a `- ` line inside an HTML comment, each count", section)
        self.assertIn("an unclosed fence runs to\n    the end of the file", section)
        self.assertIn("at least as long", section)
        self.assertIn("`갱신 YYYY-MM-DD HH:MM`", section)
        self.assertIn("surrounding\n  whitespace is ignored", section)

    def test_the_update_line_is_only_a_real_date_and_time_before_the_first_heading(self):
        self.spec(5)
        def updated(text):
            (self.root / "backlog.md").write_text(text, encoding="utf-8")
            return E._backlog(self.root)["updated"]
        self.assertEqual(updated("# B\n갱신 2026-11-21 23:10\n## C\n- a\n"), "2026-11-21 23:10")
        self.assertEqual(updated("# B\n갱신 2026-13-45 99:99\n## C\n- a\n"), "")
        self.assertEqual(updated("# B\n갱신 2026-02-30 10:00\n## C\n"), "")
        self.assertEqual(updated("# B\n갱신 2026-11-21 24:00\n## C\n"), "")
        self.assertEqual(updated("# B\n## C\n갱신 2026-11-21 23:10\n- a\n"), "")          # after the first heading
        for line in ("갱신 2026-11-21", "갱신: 2026-11-21 23:10", "Updated 2026-11-21 23:10",
                     "갱신 2026-11-21T23:10", "갱신 2026-11-21 23:10 · memo", "갱신  2026-11-21 23:10",
                     "갱신 ２０２６-11-21 23:10"):
            self.assertEqual(updated(f"# B\n{line}\n## C\n"), "", line)
        self.assertEqual(updated("# B\n갱신 2026-13-45 99:99\n갱신 2026-11-21 23:10\n## C\n"), "2026-11-21 23:10")
        self.assertEqual(updated("# B\n   갱신 2026-11-21 23:10   \n## C\n"), "2026-11-21 23:10")  # surrounding spaces ignored
        (self.root / "backlog.md").write_text("# B\n갱신 2026-13-45 99:99\n## C\n- a\n", encoding="utf-8")
        self.refresh()
        self.assertIn("1 total", self.entrance())
        self.assertNotIn("updated", self.entrance())

    def test_no_backlog_file_draws_no_section_and_changes_no_other_number(self):
        self.spec(5)
        self._routes()
        self.refresh()
        without = self.entrance()
        self.assertNotIn("Backlog", without)
        self.assertNotIn("backlog.md", self.entrance("index.html"))
        self.assertEqual(self._headings(without), ["## spec", "## Campaigns", "## Records"])
        targets = self.reports_targets()
        self.assertEqual(targets[-3:], ["all-campaigns.md", "recent-route-reports.md", "campaigns/INDEX.md"])
        self.assertNotIn("backlog.md", targets)
        (self.root / "backlog.md").write_text(BACKLOG, encoding="utf-8")
        self.refresh()
        with_file = self.reports_targets()
        self.assertEqual(with_file[:len(targets) - 2], targets[:-2])  # the numbers above it do not move
        self.assertEqual(with_file[len(targets) - 2], "backlog.md")
        self.assertEqual(with_file[len(targets) - 1:], targets[-2:])
        (self.root / "backlog.md").unlink()
        self.refresh()
        self.assertEqual(self.entrance(), without)

    def test_an_unreadable_or_oversized_backlog_keeps_one_line_without_a_count(self):
        self._setup("# B\n갱신 2026-11-21 23:10\n## C\n" + "- job\n" * 20000)
        self.assertGreater((self.root / "backlog.md").stat().st_size, E.MAX_BACKLOG_BYTES)
        readme = self.entrance()
        self.assertIn("[Open backlog.md](<backlog.md>)", readme)
        self.assertNotIn("total", readme)
        self.assertIn("Counts unavailable", readme)
        self.assertNotIn("updated", readme)
        self.assertEqual(self.entrance("index.html").count('href="backlog.md"'), 1)
        self._setup("# B\n## C\n- job\n" + "x" * (E.MAX_BACKLOG_BYTES - 15))  # exactly the limit still counts
        self.assertEqual((self.root / "backlog.md").stat().st_size, E.MAX_BACKLOG_BYTES)
        self.assertIn("1 total", self.entrance())
        path = self.root / "backlog.md"
        path.chmod(0)
        self.addCleanup(path.chmod, 0o644)
        self.refresh()
        self.assertIn("[Open backlog.md](<backlog.md>)", self.entrance())
        self.assertNotIn("total", self.entrance())
        self.assertIn("Counts unavailable", self.entrance())

    def test_a_symbolic_link_backlog_is_never_followed(self):
        self.spec(5)
        self._routes()
        outside = self.base / "elsewhere.md"
        outside.write_text(BACKLOG, encoding="utf-8")
        (self.root / "backlog.md").symlink_to(outside)
        opened = []
        real = E.open_relative
        with mock.patch.object(E, "open_relative", side_effect=lambda fd, parts, *a: (opened.append(list(parts)), real(fd, parts, *a))[1]):
            self.refresh()
        self.assertNotIn(["backlog.md"], opened)  # the link was never opened, not even to be refused
        self.assertNotIn("backlog", self.entrance().casefold())
        self.assertNotIn("backlog.md", self.entrance("index.html"))
        self.assertEqual(outside.read_text(encoding="utf-8"), BACKLOG)
        self.assertTrue((self.root / "backlog.md").is_symlink())

    def test_a_directory_named_backlog_is_no_backlog(self):
        self.spec(5)
        (self.root / "backlog.md").mkdir()
        self.refresh()
        self.assertNotIn("Backlog", self.entrance())

    def test_reports_and_open_number_reach_the_backlog_line(self):
        self._setup()
        targets = self.reports_targets()
        number = targets.index("backlog.md") + 1
        self.assertEqual(targets[number:], ["recent-route-reports.md", "campaigns/INDEX.md"])
        out = self.reports()
        self.assertIn("Backlog\n   Work handed off, and what you said then.", out)
        self.assertIn(f"{number}. Open backlog.md\n   backlog.md\n   FIX ? · IMPR ? · NOTE ? · TBD 3 · 3 total · updated 2026-11-21 23:10", out)
        self.assertEqual(self.open_line(number), str(self.root / "backlog.md"))
        self.assertEqual(self.open_line(number + 1), str(self.root / "recent-route-reports.html"))
        self.assertEqual(self.open_line(number + 2), str(self.root / "campaigns/INDEX.md"))

    def test_the_line_tells_each_sort_the_total_and_the_date(self):
        mixed = ("# B\n갱신 2026-11-21 23:10\n## C\n- a\n  kind: fix\n- b\n  kind: Improve\n- c\n  kind: note\n- d\n- e\n  kind: fix\n  kind: note\n## D\n- f\n  kind: FIX\n")
        self._setup(mixed)
        meta = "FIX 2 · IMPR 1 · NOTE 1 · TBD 2 · 6 total · updated 2026-11-21 23:10"
        self.assertIn("[Open backlog.md](<backlog.md>)\n   " + meta, self.entrance())
        self.assertIn('<span class="meta">' + meta + '</span>', self.entrance("index.html"))
        self.assertEqual(E._backlog(self.root)["kinds"], {"fix": 2, "improve": 1, "note": 1, "tbd": 2})
        # an old document (no `kind:` line anywhere): what is sorted is not told, as against none; nothing else of the line moves
        self._setup("# B\n갱신 2026-11-21 23:10\n## C\n- a\n- b\n")
        self.assertIn("\n   FIX ? · IMPR ? · NOTE ? · TBD 2 · 2 total · updated 2026-11-21 23:10", self.entrance())
        # an empty one is zeros; only notes is zeros for the rest
        self._setup("# B\n")
        self.assertIn("\n   FIX 0 · IMPR 0 · NOTE 0 · TBD 0 · 0 total", self.entrance())
        self._setup("# B\n## C\n- a\n  kind: note\n")
        self.assertIn("\n   FIX 0 · IMPR 0 · NOTE 1 · TBD 0 · 1 total", self.entrance())

    def test_the_backlog_is_read_only_and_leaves_the_file_as_it_was(self):
        self._setup()
        path = self.root / "backlog.md"
        before = (path.read_bytes(), path.stat().st_mtime_ns)
        self.refresh()
        self.reports()
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)

    def test_korean_says_the_agreed_words(self):
        self._setup()
        os.environ["FLEET_NOW_LANG"] = "ko"
        self.refresh()
        readme = self.entrance()
        self.assertIn("## 백로그", readme)
        self.assertIn("맡긴 일과 그때 하신 말. 일을 미루는 세션이 한 줄씩 적습니다.", readme)
        self.assertIn("[backlog.md 열기](<backlog.md>)\n   FIX ? · IMPR ? · NOTE ? · TBD 3 · 전체 3 · 갱신 2026-11-21 23:10", readme)
        self.assertIn("<h2>백로그</h2>", self.entrance("index.html"))

    def test_the_fallback_dictionary_carries_the_backlog_words(self):
        for key in ("entrance.section_backlog", "entrance.backlog_note", "entrance.backlog_open",
                    "entrance.backlog_count", "entrance.backlog_updated", "entrance.backlog_kinds",
                    "entrance.backlog_unavailable"):
            self.assertNotEqual(E._fallback_translator(key, count="1", date="d", fix="1", improve="1", note="1", tbd="1"), key)


def _has_entrance_marker(path):
    return E.MARKER in path.read_text(encoding="utf-8").splitlines()[:2]


if __name__ == "__main__":
    unittest.main(verbosity=2)
