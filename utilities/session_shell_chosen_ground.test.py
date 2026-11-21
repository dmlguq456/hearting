"""11-15 (owner corrections 8 and 11): the chosen choice's ground in the question popup, and the popup's
ground one cell beyond each side line.

The question editor marks every row of the chosen choice (and, while this question is answered here, the
answer being typed or picked); the popup lays one ground across those rows' content cells (`FACES.chosen`),
the cell between them and each side line keeps the popup's own ground, and where the panel shows no hue
(mono, eight colors, NO_COLOR) those same cells are reversed. The popup's ground runs one cell past each
side line on the frame's rows, where the window has that cell. These run the real backend presentation,
the real editor and the real popup paint with synthetic values; the terminal's own cells (the PTY) are
checked in `session_shell_popup.test.py`.
"""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
from pathlib import Path
import json
import subprocess
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SHELL = ROOT / "utilities/session-shell"
sys.path[:0] = [str(ROOT / "tools"), str(ROOT / "utilities")]

LONG = ("아래 두 줄 (다) — 원래 화면이 폭을 다 돌려받고, 답할 질문의 첫 줄과 실패한 route가 글로 남으며, "
        "접힌 띠는 한 줄로 서고 팝업은 그 위에 섭니다")
CHOICES = [LONG, "넓힌 옆 띠 (가)", "지금 그대로"]


def presented(popup=76, panel=52):
    """The backend's rows for a choice question (its first choice wraps), a decision, and one not
    answerable here, as the projection sends them (Korean screen)."""
    from fleet.session_block import question_presentations
    items = [{"id": "q1", "revision": "a" * 64, "answerable": True,
              "questions": [{"id": "shape", "question": "접힌 띠를 어느 안으로 구현할까요?", "choices": CHOICES, "recommended": 0}]},
             {"id": "q2", "revision": "b" * 64, "answerable": True, "answer_kind": "decision",
              "questions": [{"id": "go", "question": "이 PR을 병합해도 될까요?"}]},
             {"id": "q3", "revision": "c" * 64, "answerable": False, "reason": "other_session",
              "questions": [{"id": "far", "question": "다른 자리의 질문입니다.", "choices": ["예", "아니오"]}]}]
    with mock.patch.dict(os.environ, {"FLEET_NOW_LANG": "ko"}):
        question_presentations(items, popup, brief_columns=panel - 4)
    for item in items:
        for question in item["questions"]:
            question["display"] = {key: ([[{"text": t, "key": k} for t, k in line] for line in value]
                                         if key not in ("head", "brief_line") else [{"text": t, "key": k} for t, k in value])
                                   for key, value in question["display"].items()}
    return items


SCRIPT = r"""
const assert=require('node:assert/strict'),path=require('node:path'),v=require(path.join(process.argv[1],'panel-view.cjs'));
const create=require(path.join(process.argv[1],'block-input.cjs'));
const items=JSON.parse(process.argv[2]),F=v.FACES.dark,L=v.FACES.light;
// The FACES constant (item 8): the dark face's and the light face's chosen ground, apart from the popup's.
assert.equal(F.chosen,52);assert.equal(L.chosen,223);assert.notEqual(F.chosen,F.popup);assert.notEqual(L.chosen,L.popup);
const editor=(which=items)=>{const b=create({changed(){},answer:async()=>({state:'submitted'}),action(){}});
  b.update(which,'',{labels:{questions:'QUESTIONS'},styles:{}});b.enter();return b;};
const key=(b,s)=>b.input(Buffer.from(s));
const draw=(b,{cols=120,rows=40,colors=256,noColor=false,configuration={},background=''}={})=>{
  const W=v.popupWidth(cols),inner=Math.max(0,W-4),room=v.popupRoom(rows,0),all=b.detail({budget:room+1,columns:inner});
  return v.popup({cols,rows,band:0,title:all[0],body:all.slice(1),sections:['QUESTIONS'],current:0,
    model:{colors,no_color:noColor},configuration,background});};
// Each row of the frame: its words, its side lines' parts, its content parts (what is between `│ ` and ` │`).
const frame=r=>r.rows.slice(r.frame.top-r.top,r.frame.top-r.top+r.frame.height).map(row=>{
  const cells=row.filter(p=>!p.margin),open=cells.findIndex(p=>/^│/.test(p.text)),shut=cells.findLastIndex(p=>/│$/.test(p.text));
  return {text:cells.map(p=>p.text).join(''),row,margins:row.filter(p=>p.margin),
    sides:open>=0?[cells[open],cells[shut]]:[],content:open>=0?cells.slice(open+1,shut):[]};});
const chosen=(r,test=p=>p.background===F.chosen)=>frame(r).filter(x=>x.content.length&&x.content.every(test));
const words=x=>x.content.map(p=>p.text).join('').trim();
const out={};
// A choice question (its first choice wraps): every row of the recommended choice on the chosen ground,
// every other row of the inside on the popup's; the side lines (and the cell between each and the
// content: `│ `, ` │`) on the popup's; ↓ moves the ground to the next choice.
{const b=editor(),r=draw(b),lit=chosen(r);
 out.wrapped=lit.map(words);
 out.beside=lit.every(x=>x.sides.length===2&&x.sides.every(p=>p.background===F.popup&&/^│ $| │$/.test(p.text)));
 out.others=frame(r).filter(x=>x.content.length&&!lit.some(y=>y.text===x.text)).every(x=>x.content.every(p=>p.background===F.popup));
 out.mark=lit[0]&&lit[0].content.some(p=>p.text==='▸'&&p.bold===true);
 out.no_other_ground=frame(r).every(x=>x.content.every(p=>[F.popup,F.chosen].includes(p.background)&&p.reverse!==true));
 out.margins=frame(r).every(x=>x.margins.length===2&&x.margins.every(p=>p.background===F.popup&&p.text===' '));
 out.above_below=[r.rows[0],r.rows.at(-1)].every(row=>row.every(p=>p.background===undefined||p.background===null));
 key(b,'\x1b[B');out.moved=chosen(draw(b)).map(words);
 // A free-text question keeps the selected choice and its typed answer on the chosen ground.
 const writable=JSON.parse(JSON.stringify(items));writable[0].questions[0].free_text=true;
 const writing=editor(writable);key(writing,'2ab');out.typing=chosen(draw(writing)).map(words);
 // The light face: the same cells on its own chosen ground, the popup on its own.
 const light=draw(editor(),{background:'light'});
 out.light=chosen(light,p=>p.background===L.chosen).map(words);
 out.light_others=frame(light).filter(x=>x.content.length&&!x.content.every(p=>p.background===L.chosen)).every(x=>x.content.every(p=>p.background===L.popup));
 // No hue (mono, eight colors, NO_COLOR): those cells reversed, nothing else; no ground anywhere.
 out.plain=[{configuration:{theme:'mono'}},{colors:8},{noColor:true}].map(o=>{const p=draw(editor(),o);
   return {rev:chosen(p,q=>q.reverse===true).map(words),
     rest:frame(p).filter(x=>x.content.length&&!x.content.every(q=>q.reverse===true)).every(x=>x.content.every(q=>q.reverse!==true)),
     grounds:p.rows.flat().filter(q=>Number.isInteger(q.background)).length};});}
// A decision: nothing chosen until ↑↓ picks one, then that one.
{const b=editor([items[1]]);out.decision_before=chosen(draw(b)).map(words);key(b,'\x1b[B');out.decision=chosen(draw(b)).map(words);}
// A question not answerable here: no chosen ground at all.
{const b=editor([items[2]]),r=draw(b);out.far=r.rows.flat().filter(p=>p.background===F.chosen).length;}
// At the window's edge (under 28 columns the frame takes the whole window): no cell beyond the side
// lines, so no margin; the window one wider than the frame on one side only gets one there.
{const r=draw(editor(),{cols:24,rows:30});out.edge=[r.left,r.width,r.frame.width,r.rows.flat().filter(p=>p.margin).length];
 out.edge_chosen=chosen(r).length;}
console.log(JSON.stringify(out));
"""


class ChosenGroundTest(unittest.TestCase):
    def run_script(self):
        result = subprocess.run(["node", "-e", SCRIPT, str(SHELL), json.dumps(presented(), ensure_ascii=False)],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return json.loads(result.stdout.strip().splitlines()[-1])

    def test_the_chosen_choice_stands_on_its_own_ground_every_row_one_cell_in_from_the_lines(self):
        out = self.run_script()
        self.assertGreaterEqual(len(out["wrapped"]), 2, out["wrapped"])     # the wrapped choice, every row
        self.assertTrue(out["wrapped"][0].startswith("▸ 1  아래 두 줄"), out["wrapped"])
        self.assertTrue(out["mark"], out)                                     # ▸ and bold stay
        self.assertTrue(out["beside"], out)                                   # the cell beside each side line: the popup's
        self.assertTrue(out["others"], out)
        self.assertTrue(out["no_other_ground"], out)                          # two grounds inside, nothing reversed
        self.assertEqual(len(out["moved"]), 1, out["moved"])
        self.assertTrue(out["moved"][0].startswith("▸ 2  넓힌 옆 띠"), out["moved"])
        self.assertEqual(out["typing"], out["moved"] + ["› ab"])

    def test_the_light_face_and_the_faces_without_hue(self):
        out = self.run_script()
        self.assertEqual(out["light"], out["wrapped"])
        self.assertTrue(out["light_others"], out)
        for plain in out["plain"]:
            self.assertEqual(plain["rev"], out["wrapped"], plain)
            self.assertTrue(plain["rest"], plain)
            self.assertEqual(plain["grounds"], 0, plain)

    def test_a_decision_waits_for_its_pick_and_another_places_question_has_none(self):
        out = self.run_script()
        self.assertEqual(out["decision_before"], [])
        self.assertEqual(len(out["decision"]), 1, out["decision"])
        self.assertEqual(out["far"], 0)

    def test_the_popup_ground_runs_one_cell_past_each_side_line_where_the_window_has_it(self):
        out = self.run_script()
        self.assertTrue(out["margins"], out)                                  # left and right, every frame row
        self.assertTrue(out["above_below"], out)                              # the empty rows keep the terminal's
        self.assertEqual(out["edge"], [0, 24, 24, 0])                         # the whole window: no margin
        self.assertGreaterEqual(out["edge_chosen"], 1)


if __name__ == "__main__":
    unittest.main()
