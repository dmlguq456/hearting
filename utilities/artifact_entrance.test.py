#!/usr/bin/env python3
"""Root read-entrance tests: determinism, ownership, read-only, rows, links, CLI."""
import hashlib
import itertools
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import artifact_entrance as E  # noqa: E402

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


class LimitTests(EntranceFixture):
    def test_recent_list_stops_at_twenty_and_campaigns_at_ten(self):
        for index in range(30):
            day = f"2026-02-{index + 1:02d}"
            campaign = f"{day}_campaign-{index:02d}" if index < 12 else "2026-01-01_shared"
            locator = f"{day}_cycle-{index:02d}"
            self.cycle(campaign, locator, [("artifacts/final_report.md", f"# Doc {index}\n\nBody.\n")],
                       sealed_on=f"{day}T00:00:00Z")
        snapshot = E.scan(self.root)
        self.assertEqual(len(snapshot["recent"]), E.RECENT_LIMIT)
        self.assertEqual(len(snapshot["campaigns"]), E.CAMPAIGN_LIMIT)
        self.assertEqual(len(snapshot["all_campaigns"]), 13)

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
        campaign_items = [line for line in entrance.splitlines() if line.startswith("- [")]
        self.assertEqual(len(campaign_items), E.CAMPAIGN_LIMIT)
        self.assertIn("all-campaigns.md", entrance)


class LinkTests(EntranceFixture):
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
        self.assertIn("Recent documents", self.entrance())
        os.environ["FLEET_NOW_LANG"] = "ko"
        E.refresh(self.root)
        self.assertIn("최근 문서", self.entrance())
        self.assertIn('lang="ko"', self.entrance("index.html"))


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
        self.assertEqual(len(lines), 3)
        self.assertIn("1. 2026-01-04", lines[0])
        entrance = self.entrance()
        self.assertIn("1. 2026-01-04", entrance)
        self.assertIn("3. 2026-01-02", entrance)
        self.assertIn(str(self.root / "README.md"), done.stdout)

    def test_open_number_prints_that_bar_rows_absolute_path(self):
        self._three_documents()
        done = self._run("open", "2")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout.strip(), str(self.root / "campaigns/2026-01-03_two/2026-01-03_beta/artifacts/final_report.md"))

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
        self.assertEqual(len([line for line in several.stdout.splitlines() if re.match(r"^\d+\. ", line)]), 3)

    def test_an_out_of_range_number_is_searched_as_words(self):
        self._three_documents()
        done = self._run("open", "99")
        self.assertEqual(done.returncode, 1)
        self.assertEqual(done.stdout.strip(), "")

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
