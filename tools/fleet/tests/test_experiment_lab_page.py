"""The project page's LAB table reads the shared board and shows what the catalog says.

Fixtures are real catalogs and public run records written into a ``TemporaryDirectory``
(``utilities/experiment_catalog.test.py`` helpers); nothing reads a real artifact root.  The page is
drawn through the same ``draw`` the explorer uses, so the table is checked as it would be seen.
"""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../../run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
from dataclasses import replace
import importlib.util
from pathlib import Path
import unittest
from unittest import mock

from tools.fleet.explorer.frame import draw
from tools.fleet.explorer.state import NavState
from tools.fleet.explorer.view_model import load_fixture

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "utilities"))
import experiment_board as B  # noqa: E402
import experiment_catalog as E  # noqa: E402

_spec = importlib.util.spec_from_file_location("experiment_catalog_test_base", ROOT / "utilities" / "experiment_catalog.test.py")
T = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(T)

FIXTURE = Path(__file__).parent / "fixtures/explorer/view_model.json"
HEALTHY = {"state": "ok", "seconds_since_anchor": 60.0}
GROUP = "소리 분리 개선"
CHANGE = "잔향 제거를 켠 조건: 입력 앞 단계에 필터 하나 추가"


def pane(frame):
    return "\n".join("".join(cell[0] for cell in row[34:]) for row in frame.cells[4:-4])


class LabPageTest(T.Base):
    def setUp(self):
        super().setUp()
        self.lang = mock.patch.dict("os.environ", {"FLEET_NOW_LANG": "Korean"})
        self.lang.start()
        self.addCleanup(self.lang.stop)
        self.root = self.make_root()
        spec = {"groups": [{"display_name": GROUP, "goal": "합성 잔향 조건에서 SI-SDR이 기준 8.0 dB보다 높은가",
                            "metric": {"key": "si_sdr", "unit": "dB", "direction": "max", "split": "validation", "scoring": "합성 검증 세트"},
                            "target": {"kind": "fixed", "label": "fix", "value": 8.0, "split": "validation", "scoring": "합성 검증 세트"},
                            "status": "running"}],
                "conditions": [{"display_name": name, "group": GROUP, "changes": CHANGE if i == 1 else "",
                                "refs": {"runs": [{"run_id": f"run-{i}"}]}}
                               for i, name in enumerate(("기본 구성", "잔향 줄임", "잡음 줄임", "긴 이름을 가진 아주 오랜 시간 돌리는 조건 번호 다섯"))]}
        self.register(self.root, spec)
        for i, value in enumerate((8.4, 9.1, 8.8, 8.6)):
            self.write_run(self.root, f"run-{i}", key="si_sdr", series={"si_sdr": [(5, value)]}, stagnation=HEALTHY,
                           reached=36, total=60)
        vm = load_fixture(FIXTURE)
        self.ident = str(vm.repos[0]["id"])
        sessions = tuple(row for row in vm.sessions if row.get("repo_id", row.get("repo")) == self.ident)[:1]
        self.vm = replace(vm, sessions=sessions, clock=NOW_EPOCH)
        self.state = NavState(scope="repo:" + self.ident)

    def board(self, now=None):
        return B.board(E.status(self.root, now=NOW_EPOCH if now is None else now))

    def page(self, lab=None, cols=168, rows=70):
        repo = dict(self.vm.repos[0])
        if lab is not None:
            repo["lab"] = lab
        vm = replace(self.vm, repos=(repo,) + tuple(self.vm.repos[1:]))
        return pane(draw(vm, self.state, cols, rows, beat=1))

    def test_wide_page_shows_goal_metric_base_and_ten_columns_from_the_catalog(self):
        text = self.page(self.board())
        self.assertIn("── LAB", text)
        for value in ("합성 잔향 조건에서 SI-SDR이 기준 8.0 dB보다 높은가", "si_sdr (dB) ↑", "fix 8.00 dB",
                      "기본 구성", "잔향 줄임", "9.10@5", "+1.10 dB", "36/60 ep"):
            self.assertIn(value, text)
        for column in ("조건", "상태", "진행", "최근", "최고", "기준", "차이", "예상", "진행뒤", "관측뒤"):
            self.assertIn(column, text)
        self.assertNotIn("run-1", text)  # a registered run is called by its name
        order = [text.index(name) for name in ("기본 구성", "잔향 줄임", "잡음 줄임")]
        self.assertEqual(order, sorted(order))  # catalog order, not value order
        self.assertLess(text.index("── STATUS") if "── STATUS" in text else 0, text.index("── LAB"))
        self.assertLess(text.index("── LAB"), text.index("── OPEN WORK") if "── OPEN WORK" in text else 10 ** 9)

    def test_columns_leave_whole_in_the_documented_order(self):
        keep = ("최근", "기준", "최고", "예상", "진행뒤", "관측뒤")    # the one that stays longest first
        shown = []
        for cols in (168, 140, 120, 110, 100, 96):
            table = [line for line in self.page(self.board(), cols=cols).splitlines() if "조건" in line and "상태" in line]
            self.assertTrue(table, cols)
            header = table[0].split("── ")[0]
            shown.append([c for c in keep if c in header])
        self.assertEqual(shown[0], list(keep))                  # the widest page has every column
        for columns in shown:
            # a column is present only when every column that outlasts it is present: they leave whole, in order
            self.assertEqual(columns, list(keep[:len(columns)]), columns)
        self.assertLess(len(shown[-1]), len(shown[0]))          # the narrowest page really dropped some
        self.assertGreaterEqual(len(shown[-1]), 1)

    def test_narrow_page_keeps_name_state_progress_latest_diff_and_a_detail_line(self):
        text = self.page(self.board(), cols=98)
        for value in ("기본 구성", "잔향 줄임", "+1.10 dB"):
            self.assertIn(value, text)
        self.assertNotIn("관측뒤", text)
        self.assertIn("상세", text)  # DETAIL for the "look here" condition
        for line in text.splitlines():
            self.assertLessEqual(len(line.rstrip()), 98 - 34 + 2)

    def test_rename_reaches_the_page_on_the_next_read(self):
        B.clear_cache()
        first, _ = B.read(self.root, now=NOW_EPOCH)
        self.assertIn("잔향 줄임", self.page(first))
        E.run_write(self.root, lambda ctx: E.op_rename(ctx, "잔향 줄임", "잔향 제거 켬"), now=NOW_EPOCH + 1)
        second, _ = B.read(self.root, now=NOW_EPOCH + 2)
        text = self.page(second)
        self.assertIn("잔향 제거 켬", text)
        self.assertNotIn("잔향 줄임", text)

    def test_failure_stagnation_and_unknown_are_words_not_colors(self):
        self.write_run(self.root, "run-1", status="failed", key="si_sdr", series={"si_sdr": [(5, 9.1)]}, stagnation=HEALTHY)
        self.write_run(self.root, "run-2", key="si_sdr", series={"si_sdr": [(5, 8.8)]},
                       stagnation={"state": "stagnant", "seconds_since_anchor": 9000.0})
        (self.root / "resource-runs" / "run-3.json").unlink()
        text = self.page(self.board())
        for word in ("실패", "정체", "미확인", "실행 기록 없음"):
            self.assertIn(word, text)

    def test_not_comparable_and_no_value_show_a_reason_not_a_zero(self):
        bad = self.make_root("bad", "w")
        self.register(bad, {"groups": [{"display_name": GROUP, "goal": "g", "status": "running",
                                        "metric": {"key": "si_sdr", "unit": "dB", "direction": "max", "split": "validation", "scoring": "합성 검증 세트"},
                                        "target": {"kind": "fixed", "label": "fix", "value": 8.0, "split": "validation", "scoring": "다른 채점"}}],
                            "conditions": [{"display_name": "채점이 다른 조건", "group": GROUP, "refs": {"runs": [{"run_id": "bad-0"}]}},
                                           {"display_name": "값이 아직 없는 조건", "group": GROUP, "refs": {"runs": [{"run_id": "bad-1"}]}}]})
        self.write_run(bad, "bad-0", key="si_sdr", series={"si_sdr": [(5, 9.0)]}, stagnation=HEALTHY)
        self.write_run(bad, "bad-1", key="si_sdr", series={}, stagnation=HEALTHY)
        text = self.page(B.board(E.status(bad, now=NOW_EPOCH)))
        self.assertIn("비교 불가", text)
        self.assertIn("비교 기준의 채점 방식이 다름", text)
        self.assertIn("아직 없음", text)
        self.assertNotIn("+0.", text)
        self.assertNotIn(" 0.00 dB", text)

    def test_hidden_when_there_is_nothing_and_unconfirmed_when_unreadable(self):
        quiet = B.board(E.status(self.make_root("empty", "z"), now=NOW_EPOCH))
        self.assertNotIn("── LAB", self.page(quiet))
        self.assertNotIn("── LAB", self.page(None))
        broken = self.make_root("broken", "y")
        path = E.catalog_path(broken)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{no", encoding="utf-8")
        text = self.page(B.board(E.status(broken, now=NOW_EPOCH)))
        self.assertIn("── LAB", text)
        self.assertIn("읽을 수 없음", text)
        with mock.patch.object(E, "status", side_effect=RuntimeError("x")):
            result, _ = B.read(self.root, now=NOW_EPOCH + 500, cache={})
        self.assertIn("실험이 없다는 뜻이 아님", self.page(result))

    def test_unnamed_runs_are_listed_below_the_table_with_a_count(self):
        self.write_run(self.root, "mpwav-20261111-stray")
        text = self.page(self.board())
        self.assertIn("이름 없는 실행 1건: mpwav-20261111-stray", text)

    def test_the_condition_without_a_name_is_not_called_by_its_id(self):
        board = self.board()
        board["groups"][0]["rows"][0] = dict(board["groups"][0]["rows"][0], name=None)
        text = self.page(board)
        self.assertIn("(이름 없음)", text)
        self.assertNotIn("cnd_", text)

    def test_english_page_uses_the_same_catalog_values(self):
        with mock.patch.dict("os.environ", {"FLEET_NOW_LANG": "English"}):
            text = self.page(self.board())
        for value in ("GOAL", "METRIC", "BASE", "CONDITION", "9.10@5", "+1.10 dB", "잔향 줄임"):
            self.assertIn(value, text)


NOW_EPOCH = T.NOW


class NoLeftovers(T.Base):
    def test_zz_no_child_process_is_left(self):
        with self.assertRaises(ChildProcessError):
            os.waitpid(-1, os.WNOHANG)


if __name__ == "__main__":
    unittest.main()
