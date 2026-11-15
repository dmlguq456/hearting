#!/usr/bin/env python3
"""An experiment briefing reads the same everywhere: one table, one set of words, from the catalog.

The results table `leaderboard` prints is Fleet's LAB table (its `cells()`, its column words, its
number form), narrowed the way a narrow screen narrows it; the same catalog prints the same table on
every call.  The catalog's words (terms, other names, the metric's unit, direction and baseline) reach
the start context, the card guide, a notice's first line and the line under the table, only for what
is shown.  A notice's keys follow the declared language and a worker's pasted table reaches its
completion summary whole.  A project with no catalog yet is shown how to start one (advice only).

The catalog is synthetic (four conditions, two metrics, six terms, two former names) and lives in a
`TemporaryDirectory` with its own HOME; nothing reads a real artifact root, model, tmux or network.
"""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])

import importlib.util
import json
from pathlib import Path
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "tools"))

import experiment_board as B  # noqa: E402
import experiment_catalog as E  # noqa: E402
import resource_progress as rp  # noqa: E402
import session_card_format as card_format  # noqa: E402
import work_intent  # noqa: E402

_spec = importlib.util.spec_from_file_location("experiment_catalog_test_base", HERE / "experiment_catalog.test.py")
T = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(T)

NOW = T.NOW
HEALTHY = {"state": "ok", "seconds_since_anchor": 60.0}
TABLE = "hearting run experiment_catalog leaderboard"
TERMS = [{"term": "mix2", "meaning": "두 화자를 섞은 합성 입력"},
         {"term": "drop", "meaning": "학습 중 기준 신호 일부를 지우는 처리"},
         {"term": "long", "meaning": "학습 길이를 두 배로 늘림"},
         {"term": "WER", "meaning": "단어 오류율"},
         {"term": "SDR", "meaning": "신호 대 왜곡 비"},
         {"term": "toy-dev", "meaning": "합성 검증 세트 200문장"}]
REC, SEP = "인식 묶음", "분리 묶음"
SPEC = {"terms": TERMS,
        "groups": [{"display_name": REC, "goal": "mix2 입력에서 WER이 기준 12.00%보다 낮은가",
                    "metric": {"key": "WER", "unit": "%", "direction": "min", "split": "validation", "scoring": "toy-dev"},
                    "target": {"kind": "fixed", "label": "fix", "value": 12.0, "split": "validation", "scoring": "toy-dev"},
                    "status": "running"},
                   {"display_name": SEP, "goal": "drop을 켜면 SDR이 기본 구성보다 높은가",
                    "metric": {"key": "SDR", "unit": "dB", "direction": "max", "split": "validation", "scoring": "toy-dev"},
                    "target": {"kind": "condition", "condition": "기본 구성"}, "status": "running"}],
        "conditions": [{"display_name": "mix2 기본", "group": REC, "refs": {"runs": [{"run_id": "toy-run-a"}]}},
                       {"display_name": "mix2 long", "group": REC, "aliases": ["L2x"], "refs": {"runs": [{"run_id": "toy-run-b"}]}},
                       {"display_name": "기본 구성", "group": SEP, "refs": {"runs": [{"run_id": "toy-run-c"}]}},
                       {"display_name": "drop 켬", "group": SEP, "refs": {"runs": [{"run_id": "toy-run-d"}]}}]}


class Briefing(T.Base):
    def setUp(self):
        super().setUp()
        lang = mock.patch.dict(os.environ, {"FLEET_NOW_LANG": "Korean"})
        lang.start()
        self.addCleanup(lang.stop)
        self.root = self.make_root()
        self.register(self.root, SPEC)
        E.run_write(self.root, lambda ctx: E.op_rename(ctx, "mix2 기본", "mix2 기준선"), now=NOW + 1)   # former name 1
        E.run_write(self.root, lambda ctx: E.op_rename(ctx, "drop 켬", "drop 0.3"), now=NOW + 2)       # former name 2
        for run_id, key, series in (("toy-run-a", "WER", [(10, 12.4), (12, 12.3)]), ("toy-run-b", "WER", [(10, 11.9), (12, 11.6)]),
                                    ("toy-run-c", "SDR", [(12, 8.25)]), ("toy-run-d", "SDR", [(10, 8.7), (12, 9.05)])):
            self.write_run(self.root, run_id, key=key, series={key: series}, stagnation=HEALTHY)

    def table(self, lang="ko"):
        return E.render_leaderboard(E.status(self.root, now=NOW), None, "md", lang, E.load(self.root).doc)

    def block(self, text, group):
        return next(b for b in text.split("\n\n") if b.startswith(f"### {group} "))


class OneTable(Briefing):
    def test_the_conversation_table_is_the_fleet_lab_table_narrowed_and_the_same_on_every_call(self):
        from fleet import session_lab
        from fleet.explorer import lab_table
        from fleet.explorer.grid import CellGrid
        text = self.table()
        self.assertEqual(text, self.table())                                   # the same catalog, the same table
        columns = E.chat_columns()
        self.assertEqual(columns, ("name",) + tuple(c for c in lab_table.COLUMNS if c not in lab_table.DROP_ORDER[:2]))
        board = B.board(E.status(self.root, now=NOW))
        group = next(g for g in board["groups"] if g["name"] == REC)
        block = self.block(text, REC).splitlines()
        self.assertEqual(block[2], "| " + " | ".join(lab_table._header(c) for c in columns) + " |")
        self.assertEqual(block[2], "| 조건 | 상태 | 진행 | 최근 | 최고 | 기준 | 차이 | 예상 |")
        grid = CellGrid(200, 40)
        lab_table.draw_lab(grid, board, 0, 200, 0, NOW)
        fleet = "\n".join(grid.rows_text())
        view = session_lab.view(board, NOW)
        for row in group["rows"]:
            cells = lab_table.cells(row, group, NOW)
            line = "| " + " | ".join(cells[c][0] for c in columns) + " |"
            self.assertIn(line, block)                                         # every cell is Fleet's own
            for column in columns:
                self.assertIn(cells[column][0], fleet)                         # and Fleet shows the same words
            panel = next(item for item in view["rows"] if item["full"] == row["name"])
            for column in ("progress", "latest", "delta", "state"):
                self.assertEqual(panel["cells"][column]["text"], cells[column][0])   # and so does the shell panel
        for header in (lab_table._header(c) for c in columns):
            self.assertIn(header, fleet)
        self.assertNotIn("%@", text)                                           # the unit is on the metric line, as in Fleet
        self.assertIn("| mix2 long | 진행 | 12/40 ep | 11.60@12 | 11.60@12 | fix 12.00% | -0.4%p |", text)
        english = self.table("en")
        self.assertIn("| CONDITION | STATE | PROGRESS | LATEST | BEST | BASE | DIFF | EXPECT |", english)
        self.assertEqual(os.environ["FLEET_NOW_LANG"], "Korean")                # the screen language is put back

    def test_the_metric_line_is_fleets_metric_and_base_rows_in_one_line(self):
        from fleet.explorer import lab_table
        from fleet.explorer.grid import CellGrid
        text = self.table()
        self.assertEqual(self.block(text, REC).splitlines()[1], "지표: WER (%) ↓ · validation · toy-dev · 기준: fix 12.00%")
        self.assertEqual(self.block(text, SEP).splitlines()[1], "지표: SDR (dB) ↑ · validation · toy-dev · 기준: "
                         + lab_table.ui_text("fleet.lab.base_condition", name="기본 구성"))
        grid = CellGrid(200, 40)
        lab_table.draw_lab(grid, B.board(E.status(self.root, now=NOW)), 0, 200, 0, NOW)
        fleet = "\n".join(grid.rows_text())
        self.assertIn("WER (%) ↓ · validation · toy-dev", fleet)                # Fleet's METRIC row says the same
        self.assertIn("fix 12.00%", fleet)

    def test_the_words_line_says_only_what_the_table_shows_and_leads_old_names_to_the_current(self):
        rec, sep = self.block(self.table(), REC), self.block(self.table(), SEP)
        words = rec.splitlines()[-1]
        self.assertTrue(words.startswith("- 낱말: ↓ 낮을수록 좋음 · %p 퍼센트포인트"), words)
        for said in ("mix2 기본 → mix2 기준선", "L2x → mix2 long", "mix2: 두 화자를 섞은 합성 입력", "long: 학습 길이를 두 배로 늘림",
                     "WER: 단어 오류율", "toy-dev: 합성 검증 세트 200문장"):
            self.assertIn(said, words)
        for unsaid in ("drop:", "SDR:", "drop 켬"):
            self.assertNotIn(unsaid, words)                                    # the other group's words stay there
        self.assertIn("drop 켬 → drop 0.3", sep.splitlines()[-1])
        self.assertIn("SDR: 신호 대 왜곡 비", sep.splitlines()[-1])
        self.assertNotIn("%p 퍼센트포인트", sep)                                 # no %p value there, no %p word
        self.assertEqual(E.resolve(E.load(self.root).doc, "drop 켬")["display_name"], "drop 0.3")   # the old name still finds it
        for line in rec.splitlines()[4:-1]:
            self.assertTrue(line.startswith("| "), line)                        # rows, then the words: no blank line inside


class WordsEverywhere(Briefing):
    def test_start_context_and_card_guide_carry_the_table_command_metric_line_and_words_inside_their_bytes(self):
        doc = E.load(self.root).doc
        path = str(E.catalog_path(self.root))
        start = E.catalog_brief(doc, path, 360, "ko")
        guide = E.catalog_brief(doc, path, 600, "ko")
        self.assertLessEqual(len(start.encode("utf-8")), 360)
        self.assertLessEqual(len(guide.encode("utf-8")), 600)
        for brief in (start, guide):
            self.assertIn(TABLE, brief)
            self.assertIn("지표: WER (%) ↓", brief)
            self.assertIn("기준: fix 12.00%", brief)
            self.assertIn("mix2 기준선", brief)
            self.assertNotIn("mix2 기본,", brief)                                # the former name is never the name
        self.assertIn("낱말: mix2 기본 → mix2 기준선", guide)
        one = self.make_root("one", "b")                                       # one group: room for its words at 360
        self.register(one, {"terms": TERMS, "groups": [SPEC["groups"][0]], "conditions": SPEC["conditions"][:2]})
        E.run_write(one, lambda ctx: E.op_rename(ctx, "mix2 기본", "mix2 기준선"), now=NOW + 1)
        small = E.catalog_brief(E.load(one).doc, str(E.catalog_path(one)), 360, "ko")
        self.assertLessEqual(len(small.encode("utf-8")), 360)
        self.assertIn("낱말: mix2 기본 → mix2 기준선 · L2x → mix2 long", small)
        written = card_format._catalog_guide(str(self.root.parent))
        self.assertIn("write results as its table", written)
        self.assertIn(TABLE, written)
        self.assertLessEqual(len(written.encode("utf-8")), card_format.CATALOG_GUIDE_BYTES + 200)

    def test_a_notice_leads_with_the_tables_metric_line_and_words_and_ends_with_the_table_command(self):
        summary = T.summary_of({"WER": [(12, 11.6)]}, key="WER")
        run = {"run_id": "toy-run-b", "row_id": "arm", "artifact_root": str(self.root)}
        observation = {"summary": summary, "reached": 12, "total": 40, "unit": "epoch", "eta_seconds": 600.0, "at": NOW}
        context = rp._experiment_context(run, "toy-run-b", observation, True)
        texts = rp._notice_texts(run, "checkpoint", observation, None, [],
                                 rp._experiment_lines(context, first=True, final=False),
                                 rp._experiment_optional(context, first=True, final=False))
        first = texts[0]
        self.assertTrue(first.startswith("mix2 long — mix2 입력에서 WER이 기준 12.00%보다 낮은가; 최근 11.60%@12; "
                                         "fix 12.00% 대비 -0.4%p"), first)
        self.assertIn("; 지표: WER (%) ↓ · validation · toy-dev · 기준: fix 12.00%; 낱말: L2x → mix2 long", first)
        self.assertIn("실행=toy-run-b 행=arm", first)                          # the trace stays, in the reader's words
        self.assertTrue(texts[-1].endswith(f"표: {TABLE} --group '{REC}'"), texts[-1])
        self.assertEqual(len(texts), 1)                                         # the optional pieces never add a part
        self.assertLessEqual(len(texts[0]), 600)
        long = rp._notice_texts(run, "checkpoint", observation, "/r/" + "x" * 400 + ".json", [],
                                rp._experiment_lines(context, first=True, final=False),
                                rp._experiment_optional(context, first=True, final=False))
        bare = rp._notice_texts(run, "checkpoint", observation, "/r/" + "x" * 400 + ".json", [],
                                rp._experiment_lines(context, first=True, final=False))
        self.assertEqual(long, bare)                                           # a split notice is as it was
        self.assertEqual(E.scan_text("\n".join(texts), E.load(self.root).doc), [])   # nothing for the name check to add


class NoticeLanguage(unittest.TestCase):
    def test_the_notice_keys_follow_the_declared_language_and_values_use_the_catalogs_number_form(self):
        run = {"run_id": "r1", "row_id": "w1", "status": "failed", "exit_code": 3}
        observation = {"summary": {"metric": "loss", "latest": {"value": 0.123456, "epoch": 3},
                                   "latest_metrics": {"loss": {"value": 0.123456, "epoch": 3}},
                                   "best": {"value": 2.0, "epoch": 2}, "best_metrics": {"loss": {"value": 2.0, "epoch": 2}},
                                   "trend": "악화"},
                       "elapsed_seconds": 3720, "attention": [{"kind": "metric-regression"}]}
        texts = {}
        for language in ("English", "Korean"):
            with mock.patch.dict(os.environ, {"FLEET_NOW_LANG": language}):
                texts[language] = "; ".join(rp._notice_texts(run, "final", observation, "/r/x.json", [{"format": "jsonl", "path": "/o/l"}]))
        self.assertIn("run=r1 row=w1; public run record=/r/x.json", texts["English"])
        self.assertIn("status=failed; exit=3", texts["English"])
        self.assertIn("attention=metric-regression", texts["English"])
        self.assertIn("실행=r1 행=w1; 실행 기록=/r/x.json", texts["Korean"])
        self.assertIn("상태=failed; 종료 코드=3", texts["Korean"])
        self.assertIn("주의=metric-regression", texts["Korean"])
        self.assertIn("출처 jsonl=/o/l", texts["Korean"])
        for text in texts.values():
            self.assertIn("loss=0.1235@3", text)                                # `experiment_catalog._fmt`, not the raw float
            self.assertIn("loss=2.00@2", text)
            self.assertNotIn("0.123456", text)


class WorkerTable(Briefing):
    def test_a_table_the_worker_pasted_reaches_its_completion_summary_whole(self):
        table = self.table()
        report = self.base / "final_report.md"
        report.write_text("# 실험 보고\n\n## 결과\n\n" + table + "\n\n## 검증\n- `python3 x.py` PASS\n", encoding="utf-8")
        route = {"capability": "autopilot-lab", "route_id": "rt-synthetic", "work_request": {"text": "합성 실험 보고"}}
        with mock.patch.dict(os.environ, {"FLEET_NOW_LANG": "en"}):
            summary = work_intent.completion_summary(route, str(report))
        self.assertTrue(summary.startswith(work_intent.summary_heads()))
        self.assertNotIn("\n\n", summary)                                      # still one block (`sealed_summary`)
        for group in (REC, SEP):
            self.assertIn(self.block(table, group), summary)                    # each group's block, as printed
        plain = work_intent.completion_summary(route, str(self.base / "missing.md"))
        self.assertNotIn("| 조건 |", plain)


class NoCatalogYet(T.Base):
    def test_a_project_with_runs_and_no_catalog_is_shown_how_to_start_one(self):
        root = self.make_root("bare", "c")
        self.write_run(root, "lone-run", series={"wer": [(3, 30.0)]})
        with mock.patch.dict(os.environ, {"FLEET_NOW_LANG": "Korean"}):
            text, named = E.folder_brief(str(root.parent), 360)
            self.assertFalse(named)
            self.assertIn("hearting run experiment_catalog register --group-name <묶음> --condition-name <조건>", text)
            self.assertLessEqual(len(text.encode("utf-8")), 360)
            context = rp._experiment_context({"artifact_root": str(root), "run_id": "lone-run"}, "lone-run",
                                             {"summary": T.summary_of({"wer": [(3, 30.0)]})}, False)
            pieces = rp._experiment_optional(context, first=True, final=False)
            self.assertEqual(rp._experiment_lines(context, first=True, final=False), [])    # nothing required
            self.assertIn("정본 이름 없음", pieces[0][1])
            self.assertIn("--run lone-run", pieces[0][1])                          # no decision metric declared: still told
            self.assertEqual(rp._experiment_optional(context, first=False, final=False), [])   # not on every checkpoint
            self.assertIn("register", E.render_leaderboard(E.status(root, now=NOW), lang="ko"))
        empty = self.make_root("empty", "d")
        self.assertEqual(E.folder_brief(str(empty.parent), 360), ("", False))     # no runs, nothing to say


if __name__ == "__main__":
    unittest.main()
