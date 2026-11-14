#!/usr/bin/env python3
"""One synthetic experiment, read by every consumer: the same catalog name, the same comparison.

BC-LAB-REPORT-LEGIBILITY's single comparison condition: with the same synthetic experiment, do the
board, the Fleet table, a runtime notice, `compute-hosts`, the leaderboard, a saved card, the start
context (three harnesses) and a frame question's name advice use the catalog's *current* name and the
catalog's one `compare()` result, without any hand-made substitute?  A consumer that needs its own name
table fails this test (the counter-test for "a separate name list is enough").

The unit tests of each consumer are not a substitute: this is where they meet, including the shell renderer and steward summary.

Fixtures live in temporary folders with their own HOME; no real artifact root, session, or account is read.
"""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])

import importlib.util
import json
import subprocess
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
for path in (ROOT, HERE, ROOT / "tools" / "memory" / "tests"):
    sys.path.insert(0, str(path))
os.environ["FLEET_NOW_LANG"] = "Korean"

import experiment_board as B  # noqa: E402
import experiment_catalog as E  # noqa: E402
import resource_progress as rp  # noqa: E402
import session_tidy as st  # noqa: E402
import work_start as W  # noqa: E402
from tidy_isolation import isolated_env  # noqa: E402
import pane_ownership  # noqa: E402
from tools.fleet.explorer.frame import draw  # noqa: E402
from tools.fleet.explorer.state import NavState  # noqa: E402
from tools.fleet.explorer.view_model import load_fixture  # noqa: E402

_spec = importlib.util.spec_from_file_location("experiment_catalog_test_base", HERE / "experiment_catalog.test.py")
T = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(T)

NOW = T.NOW
GROUP = "AMI 가변 화자 개선"
HEALTHY = {"state": "ok", "seconds_since_anchor": 60.0}
OLD_A, OLD_B = "옛 대조군 가", "옛 실험군 나"
NEW_A, NEW_B = "기준 새 이름", "실험 새 이름"
FIXTURE = ROOT / "tools/fleet/tests/fixtures/explorer/view_model.json"


def load_hyphen(name, filename):
    spec = importlib.util.spec_from_file_location(name, HERE / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class Consumers(T.Base):
    @staticmethod
    def reap_children():
        # `session_tidy.run_hook` may start the shared helper of its own state root; the isolation's cleanup
        # retires it.  Wait for that child here so it is not mistaken for a leftover of a later test.
        deadline = time.monotonic() + 10
        while True:
            try:
                pid, _status = os.waitpid(-1, os.WNOHANG)
            except ChildProcessError:
                break
            if pid == 0:
                if time.monotonic() > deadline:
                    break
                time.sleep(0.05)

    def project(self, epoch_b):
        """A project whose group compares `실험 조건` with its control `기준 조건`; both are renamed afterwards."""
        root = self.make_root("proj", "a")
        self.register(root, {"groups": [T.group_spec(target={"kind": "condition", "condition": OLD_A},
                                                     metric={**T.METRIC, "scoring": None})],
                             "conditions": [
                                 {"display_name": OLD_A, "group": GROUP, "refs": {"runs": [{"run_id": "run-a"}]}},
                                 {"display_name": OLD_B, "group": GROUP, "refs": {"runs": [{"run_id": "run-b"}]}}]})
        self.write_run(root, "run-a", series={"wer": [(12, 20.0)]}, stagnation=HEALTHY)
        self.write_run(root, "run-b", series={"wer": [(epoch_b - 1, 19.5), (epoch_b, 20.5)]}, stagnation=HEALTHY)
        E.run_write(root, lambda ctx: E.op_rename(ctx, OLD_A, NEW_A), now=NOW + 1)
        E.run_write(root, lambda ctx: E.op_rename(ctx, OLD_B, NEW_B), now=NOW + 2)
        return root

    def fleet_page(self, board):
        vm = load_fixture(FIXTURE)
        ident = str(vm.repos[0]["id"])
        sessions = tuple(r for r in vm.sessions if r.get("repo_id", r.get("repo")) == ident)[:1]
        repo = dict(vm.repos[0], lab=board)
        vm = replace(vm, repos=(repo,) + tuple(vm.repos[1:]), sessions=sessions, clock=NOW)
        frame = draw(vm, NavState(scope="repo:" + ident), 168, 70, beat=1)
        return "\n".join("".join(cell[0] for cell in row[34:]) for row in frame.cells[4:-4])

    def consumers(self, root):
        """`{consumer: text}` for the one renamed condition, as each consumer shows it."""
        doc = E.status(root, now=NOW + 3)
        out = {}
        board = B.board(doc)
        row = next(r for r in board["groups"][0]["rows"] if r["name"] == NEW_B)
        out["board"] = " ".join(str(v) for v in (row["name"], row["comparison_state"], row["reason"], row["delta"], row["delta_unit"]))
        out["fleet"] = self.fleet_page(board)
        epoch = 12 if row["comparison_state"] == "ok" else 10
        context = rp._experiment_context({"artifact_root": str(root), "run_id": "run-b"}, "run-b",
                                         {"summary": T.summary_of({"wer": [(epoch, 20.5)]})}, True)
        out["notice"] = "\n".join(rp._experiment_lines(context, first=True, final=False) + [
            piece(600) if callable(piece) else piece for _place, piece in rp._experiment_optional(context, first=True, final=False)])
        hosts = load_hyphen("compute_hosts_consumers", "compute-hosts.py")
        info = hosts._catalog_identity(str(root), "run-b")
        out["compute-hosts"] = f"{info['name']} {info['goal']}" if info else ""
        out["leaderboard"] = E.render_leaderboard(doc, None, "md", "ko")
        out["card"] = st.card_name_advice("## 진행 중\nrun-b 결과를 확인했다", str(root.parent))
        route = {"artifact_root": str(root)}
        question = {"understanding": "u", "questions": [{"question": "run-b 로 이어서 갈까?", "options": [{"label": "예", "means": "m"}]}]}
        fields = W._name_warning_fields(route, question).get("name_warnings", [])
        out["question-advice"] = " ".join(f"{w['token']}={w['name']}" for w in fields)
        return out, board

    def start_blocks(self, root):
        """The start context on the three harnesses, from the experiment catalog block onward."""
        self.addCleanup(self.reap_children)        # registered first, so it runs after the isolation's cleanup
        iso = isolated_env()
        self.addCleanup(iso.cleanup)
        proof = mock.patch.object(pane_ownership, "verified_pane", side_effect=lambda pane, *a, **kw: pane or "")
        proof.start()
        self.addCleanup(proof.stop)
        blocks = {}
        for harness in ("claude", "codex", "opencode"):
            with iso.patched_environ({"HERDR_PANE_ID": f"test:{harness}"}):
                text = st.run_hook(harness, "start", f"sid-{harness}", cwd=str(root.parent))
            blocks[harness] = text[text.index("결과 표"):] if "결과 표" in text else ""
        return blocks

    def test_a_renamed_condition_and_its_comparison_are_the_same_in_every_consumer(self):
        root = self.project(epoch_b=12)
        texts, board = self.consumers(root)
        row = next(r for r in board["groups"][0]["rows"] if r["name"] == NEW_B)
        self.assertEqual((row["comparison_state"], round(row["delta"], 2), row["delta_unit"]), ("ok", 0.5, "pp"))
        from fleet import session_lab
        from fleet.explorer import lab_table
        view = session_lab.view(board, NOW + 3)
        shown = next(item for item in view["rows"] if item["name"]["text"] == NEW_B)
        self.assertEqual(shown["cells"]["delta"]["text"], lab_table.cells(row, board["groups"][0], NOW + 3)["delta"][0])
        self.assertIn("+0.5", shown["cells"]["delta"]["text"])
        # A former name shows only as the way to the current one (`old → new` in the words line).
        bare = {k: v.replace(f"{OLD_B} → {NEW_B}", "").replace(f"{OLD_A} → {NEW_A}", "") for k, v in texts.items()}
        for consumer in ("board", "fleet", "notice", "compute-hosts", "leaderboard", "card"):
            self.assertIn(NEW_B, texts[consumer], consumer)
            self.assertNotIn(OLD_B, bare[consumer], consumer)
        self.assertIn(f"{OLD_B} → {NEW_B}", texts["notice"])
        for consumer in ("fleet", "notice", "leaderboard"):                      # the three that show the comparison
            self.assertIn("+0.5%p", texts[consumer], consumer)
            self.assertIn(NEW_A, texts[consumer], consumer)                      # the control carries its new name too
            self.assertNotIn(OLD_A, bare[consumer], consumer)
        self.assertIn(f"| {NEW_B} | 진행 | 12/40 ep | 20.50@12 | 19.50@11 |", texts["leaderboard"])  # latest and best are different values
        self.assertIn(f"run-b (= {NEW_B})", texts["card"])                          # the bare run id got its name, the id stays
        self.assertEqual(texts["question-advice"], f"run-b={NEW_B}")

    def test_when_epochs_differ_no_consumer_shows_a_difference_and_all_give_the_same_reason(self):
        root = self.project(epoch_b=10)
        texts, board = self.consumers(root)
        row = next(r for r in board["groups"][0]["rows"] if r["name"] == NEW_B)
        self.assertEqual((row["comparison_state"], row["reason"], row["delta"]), ("not-comparable", "epoch-mismatch", None))
        reason = _reason_text("epoch-mismatch")
        for consumer in ("fleet", "notice", "leaderboard"):
            self.assertIn(reason, texts[consumer], consumer)
            self.assertNotIn("+0.5", texts[consumer], consumer)                  # no difference anywhere
            self.assertNotIn("%p", texts[consumer].replace("비교 불가", ""), consumer)
            self.assertIn(NEW_B, texts[consumer], consumer)

    def test_the_start_context_shows_the_current_name_and_the_same_block_on_all_three_harnesses(self):
        root = self.project(epoch_b=12)
        blocks = self.start_blocks(root)
        for harness, block in blocks.items():
            self.assertIn(NEW_B, block, harness)
            self.assertNotIn(OLD_B, block.replace(f"{OLD_B} → {NEW_B}", ""), harness)
            self.assertLessEqual(len(block.encode("utf-8")), st.LAB_BLOCK_BYTES, harness)
        self.assertEqual(len({blocks["claude"], blocks["codex"], blocks["opencode"]}), 1, blocks)

    def test_a_runtime_notice_leads_with_the_name_so_the_name_check_finds_nothing_to_warn_about(self):
        # BC-LAB-REPORT-LEGIBILITY 4.3: the notice is made with the name from the start, so `scan_text`
        # over the whole text (run id and all) has no warning; the check is not part of the notice path.
        root = self.project(epoch_b=12)
        doc = E.load(root).doc
        summary = T.summary_of({"wer": [(12, 20.5)]})
        run = {"run_id": "run-b", "row_id": "run-b", "artifact_root": str(root)}
        observation = {"summary": summary, "reached": 12, "total": 40, "unit": "epoch", "eta_seconds": 600.0, "at": NOW}
        context = rp._experiment_context(run, "run-b", observation, True)
        text = "\n".join(rp._notice_texts(run, "checkpoint", observation, None, [],
                                          rp._experiment_lines(context, first=True, final=False)))
        self.assertTrue(text.startswith(NEW_B + " — "), text)
        self.assertIn("실행=run-b", text)                                  # the trace stays
        self.assertEqual(E.scan_text(text, doc), [])
        self.assertEqual({w["token"] for w in E.scan_text("run-b 결과를 확인한다", doc)}, {"run-b"})



class CrossPresentation(T.Base):
    fleet_page = Consumers.fleet_page
    def mixed(self):
        root = self.make_root()
        second = "둘째 묶음"
        def condition(name, group, run=None):
            value = {"display_name": name, "group": group}
            if run:
                value["refs"] = {"runs": [{"run_id": run}]}
            return value
        self.register(root, {"groups": [T.group_spec(), T.group_spec(display_name=second)],
                             "conditions": [condition("실패 조건", GROUP, "bad"),
                                            condition("정체 조건", GROUP, "stuck"),
                                            condition("대기 조건의 길고 자세한 한글 이름", GROUP),
                                            condition("관측 끊긴 조건", second, "stale"),
                                            condition("긴 에폭 정상", second, "live")]})
        self.write_run(root, "bad", status="failed", stagnation=HEALTHY)
        self.write_run(root, "stuck", stagnation={"state": "stagnant", "seconds_since_anchor": 9000})
        self.write_run(root, "stale", at=NOW - 1000, stagnation=HEALTHY)
        self.write_run(root, "live", at=NOW - 1000, stagnation=HEALTHY)
        self.write_run(root, "unlinked", stagnation=HEALTHY)
        # The observer keeps a living, unchanged epoch observed for twenty minutes.
        run = {"run_id": "live", "started_at": NOW - 3600, "artifact_root": str(root), "status": "running"}
        with mock.patch.object(rp, "_observation_state_path", return_value=self.base / "observation.json"):
            for step in range(0, 1201, 60):
                rp.observe_experiment(run, root / "registry.json", NOW - 1200 + step, row_key="run",
                                      started_at=NOW - 3600, status="running", notify_events=False, alive=True)
        return root

    def observe(self, root, board):
        import steward_check as SC
        with mock.patch.object(B, "read", return_value=(board, [])):
            return SC._observe_lab({str(root.parent)})

    def test_counts_names_renderer_and_steward_new_unknown(self):
        from fleet import session_lab
        from fleet.explorer import lab_table
        import steward_check as SC
        root = self.mixed()
        board = B.read(root, now=NOW, cache={})[0]
        first, second = board["groups"]
        self.assertEqual({k: board["totals"][k] for k in ("failed", "stagnant", "unknown", "waiting")},
                         {"failed": 1, "stagnant": 1, "unknown": 1, "waiting": 2})
        self.assertEqual(board["unlinked"]["count"], 1)
        self.assertEqual((first["headline"]["name"], second["headline"]["name"]), ("실패 조건", "관측 끊긴 조건"))
        rows = {row["name"]: row for g in board["groups"] for row in g["rows"]}
        self.assertFalse(rows["긴 에폭 정상"]["unknown_flag"])
        self.assertTrue(rows["관측 끊긴 조건"]["unknown_flag"])
        self.assertEqual({key: first["counts"][key] for key in ("failed", "stagnant", "unknown", "waiting")},
                         {"failed": 1, "stagnant": 1, "unknown": 0, "waiting": 1})
        self.assertEqual({key: second["counts"][key] for key in ("failed", "stagnant", "unknown", "waiting")},
                         {"failed": 0, "stagnant": 0, "unknown": 1, "waiting": 1})
        summary = lab_table.summary(board, NOW)                                  # Fleet's header: the first group's numbers
        self.assertIn("조건 3 · 실패 1 · 정체 1", summary)
        self.assertNotIn("미확인", summary)
        self.assertEqual(lab_table.cells(rows["긴 에폭 정상"], second, NOW)["state"][0], "진행")
        self.assertEqual(lab_table.cells(rows["관측 끊긴 조건"], second, NOW)["state"][0], "미확인")
        second_view = session_lab.view({**board, "selected": second["id"]}, NOW)
        self.assertEqual([p[0]["text"] for p in second_view["counts"]], ["미확인 1", "이름 없음 1", "대기 1"])
        for row, shown in zip(second["rows"], second_view["rows"]):
            self.assertEqual(shown["cells"]["state"]["text"], lab_table.cells(row, second, NOW)["state"][0])
        fleet = self.fleet_page(board)
        for name in ("실패 조건", "정체 조건", "관측 끊긴 조건", "긴 에폭 정상"):
            self.assertIn(name, fleet)
        lines = fleet.splitlines()
        self.assertIn("진행", next(line for line in lines if "긴 에폭 정상" in line))
        self.assertNotIn("미확인", next(line for line in lines if "긴 에폭 정상" in line))
        self.assertIn("미확인", next(line for line in lines if "관측 끊긴 조건" in line))
        view = session_lab.view(board, NOW)
        self.assertEqual([p[0]["text"] for p in view["counts"]], ["실패 1", "정체 1", "이름 없음 1", "대기 1"])
        self.assertEqual(view["headline"]["left"][0]["text"], first["headline"]["name"])
        for row, shown in zip(first["rows"], view["rows"]):
            cells = lab_table.cells(row, first, NOW)
            self.assertEqual(shown["name"]["text"], cells["name"][0])
            self.assertEqual(shown["cells"]["delta"]["text"], cells["delta"][0])
        self.assertEqual("".join(p["text"] for p in view["strip"]), "!2LB")
        seen = self.observe(root, board)
        line, names, notified = SC._lab_summary({"lab": seen}, [])
        self.assertIn("미확인 1·이름 없음 1·실패 1·정체 1 (묶음 2개)", line)
        # One word for the runs without a catalog name: supervisor line, shell head and Fleet's lower line.
        for lang, word in (("ko", "이름 없"), ("en", "unnamed")):
            with mock.patch.dict(os.environ, {"FLEET_NOW_LANG": lang}):
                self.assertIn(word, SC._lab_summary({"lab": seen}, [])[0])
                self.assertIn(word, "".join(p["text"] for p in session_lab.view(board, NOW)["counts"][2]))
                self.assertIn(word, lab_table.ui_text("experiment.unlinked", count=1, ids="loose-run"))
        self.assertIn("둘째 묶음 — 관측 끊긴 조건", names)
        _, fallback, _ = SC._lab_summary({"lab": seen}, notified)
        self.assertIn(GROUP + " — 실패 조건", fallback)
        script = r"""
const assert=require('node:assert/strict'),v=require(process.argv[1]),lab=JSON.parse(process.argv[2]);
const draw=(open,folded)=>v.render({width:folded?120:40,height:folded?4:60,side:!folded,fill:!folded,folded,harness:'codex',state:'idle',configuration:{},model:{lab,repo:[{text:'proj'}]},expandedKeys:open?[lab.key]:[],questions:()=>[]}).rows.map(r=>r.map(p=>p.text).join(''));
for(const open of [false,true]){const lines=draw(open,false).join('\n');assert(lines.includes('실패 조건'),lines);assert(lines.includes('3 LAB'),lines);}
// Folded, the strip under the native area ends its routes row with LAB's attention token (spec rule 59 (g)).
assert(draw(false,true).some(s=>/^!2 LAB +$/.test(s)),draw(false,true).join('\n'));
const head=draw(true,false).join('\n');assert(head.includes('3 LAB')&&head.includes('실패 1 정체 1'),head);
"""
        result = subprocess.run(["node", "-e", script, str(ROOT / "utilities/session-shell/panel-view.cjs"),
                                 json.dumps(view, ensure_ascii=False)], capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_new_unknown_selects_its_groups_headline_not_necessarily_the_unknown(self):
        import steward_check as SC
        root = self.mixed()
        self.write_run(root, "live", status="failed", stagnation=HEALTHY)
        board = B.read(root, now=NOW, cache={})[0]
        second = board["groups"][1]
        self.assertEqual(second["headline"]["name"], "긴 에폭 정상")
        self.assertTrue(next(r for r in second["rows"] if r["name"] == "관측 끊긴 조건")["unknown_flag"])
        _, names, _ = SC._lab_summary({"lab": self.observe(root, board)}, [])
        self.assertIn(second["name"] + " — " + second["headline"]["name"], names)

    def test_fallback_matches_shell_even_when_failure_is_in_second_group(self):
        from fleet import session_lab
        import steward_check as SC
        root = self.make_root()
        second = "둘째 묶음"
        self.register(root, {"groups": [T.group_spec(), T.group_spec(display_name=second)],
                             "conditions": [{"display_name": "정상 조건", "group": GROUP,
                                             "refs": {"runs": [{"run_id": "good"}]}},
                                            {"display_name": "둘째 실패", "group": second,
                                             "refs": {"runs": [{"run_id": "bad"}]}}]})
        self.write_run(root, "good", stagnation=HEALTHY)
        self.write_run(root, "bad", status="failed", stagnation=HEALTHY)
        board = B.read(root, now=NOW, cache={})[0]
        seen = self.observe(root, board)
        _, names, _ = SC._lab_summary({"lab": seen}, [])
        selected = next(g for g in board["groups"] if g["id"] == board["selected"])
        self.assertIn(selected["name"] + " — " + selected["headline"]["name"], names)
        self.assertEqual(session_lab._selected(board)["id"], selected["id"])


def _reason_text(code):
    return E._translator("ko")(f"experiment.reason.{code}")


class NoLeftovers(T.Base):
    def test_zz_no_child_process_is_left(self):
        with self.assertRaises(ChildProcessError):
            os.waitpid(-1, os.WNOHANG)


if __name__ == "__main__":
    unittest.main()
