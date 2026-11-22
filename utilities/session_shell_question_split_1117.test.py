"""11-17 ("질문이랑 선지가 잘 구분되게" / "구분선 위에 여백도 하나 두고"): the question popup parts the question from its choices.

A question with choices reads: its words, one empty row, a thin line (the popup frame's own color; dim where the frame
is bright), then the choices, each number in the question's rose and bold with two cells before the words. A question
answered in words has neither the line nor the empty row. Short of rows the empty row goes first and the line stays;
the choices and the keys are never pushed out. The chosen row keeps its mark, ground and weight. The panel's choices row
(`1 label · 2 label`) wears the same rose on its numbers. These run the real backend presentation, the real editor and
the real popup paint with synthetic values; the three harnesses' terminal cells are checked in
`session_shell_popup.test.py`.
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

LONG = ("패널을 접었을 때의 모양을 어느 안으로 구현할까요?\n"
        "- 아래 두 줄(다): 원래 화면이 폭을 다 돌려받고, 답할 질문의 첫 줄과 실패한 route가 글로 남습니다.\n"
        "- 넓힌 옆 띠(가): 규칙이 가장 적게 바뀌지만 원래 화면이 21칸을 잃습니다.\n"
        "- 아래 한 줄(나): 가장 작게 바뀝니다.\n"
        "고른 안만 따로 구현하고, 나머지는 시안으로 남깁니다.")
CHOICES = ["아래 두 줄 (다)", "넓힌 옆 띠 (가) — 원래 화면이 21칸을 잃지만 규칙이 가장 적게 바뀌고 팝업은 그대로 선다", "지금 그대로"]
SHORT = "색을 지금 그대로 둘까요, 한 단계 밝게 할까요?"


def presented(popup=76, panel=52):
    """The backend's rows for a long question with three choices, a question answered in words, a short one with
    two choices, and a decision, as the projection sends them (Korean screen)."""
    from fleet.session_block import question_presentations
    items = [{"id": "q1", "revision": "a" * 64, "answerable": True,
              "questions": [{"id": "shape", "question": LONG, "choices": CHOICES, "recommended": 0}]},
             {"id": "q2", "revision": "b" * 64, "answerable": True,
              "questions": [{"id": "color", "question": "MESSAGES 구분선의 색을 어떻게 할까요? 글로 적어 주세요."}]},
             {"id": "q3", "revision": "c" * 64, "answerable": True,
              "questions": [{"id": "tone", "question": SHORT, "choices": ["그대로", "한 단계 밝게"]}]},
             {"id": "q4", "revision": "d" * 64, "answerable": True, "answer_kind": "decision",
              "questions": [{"id": "go", "question": "이 PR을 병합해도 될까요?"}]}]
    with mock.patch.dict(os.environ, {"FLEET_NOW_LANG": "ko"}):
        question_presentations(items, popup, brief_columns=panel - 4)
    for item in items:
        for question in item["questions"]:
            question["display"] = {key: ([[{"text": t, "key": k} for t, k in line] for line in value]
                                         if key not in ("head", "brief_line") else [{"text": t, "key": k} for t, k in value])
                                   for key, value in question["display"].items()}
    return items


def styles():
    from fleet.palette import style_components
    keys = ("dim", "loc_repo", "g_blocked", "harness", "lvl_g", "g_idle")
    return {key: style_components(key, colors=256, no_color=False) for key in keys}


SCRIPT = r"""
const assert=require('node:assert/strict'),path=require('node:path'),v=require(path.join(process.argv[1],'panel-view.cjs'));
const create=require(path.join(process.argv[1],'block-input.cjs'));
const items=JSON.parse(process.argv[2]),styles=JSON.parse(process.argv[3]),F=v.FACES.dark,L=v.FACES.light;
const editor=(which=items,sty=styles)=>{const b=create({changed(){},answer:async()=>({state:'submitted'}),action(){}});
  b.update(which,'',{labels:{questions:'QUESTIONS'},styles:sty});b.enter();return b;};
const key=(b,s)=>b.input(Buffer.from(s));
const draw=(b,{cols=120,rows=40,colors=256,noColor=false,configuration={},background=''}={})=>{
  const W=v.popupWidth(cols),inner=Math.max(0,W-4),room=v.popupRoom(rows,0),all=b.detail({budget:room+1,columns:inner});
  return v.popup({cols,rows,band:0,title:all[0],body:all.slice(1),sections:['QUESTIONS'],current:0,
    model:{colors,no_color:noColor},configuration,background});};
const frame=r=>r.rows.slice(r.frame.top-r.top,r.frame.top-r.top+r.frame.height).map(row=>{
  const cells=row.filter(p=>!p.margin),open=cells.findIndex(p=>/^│/.test(p.text)),shut=cells.findLastIndex(p=>/│$/.test(p.text));
  return {row,content:open>=0?cells.slice(open+1,shut):[],text:cells.map(p=>p.text).join('')};});
const words=x=>x.content.map(p=>p.text).join('').replace(/^ /,'').replace(/ +$/,'');
const ruleRow=x=>x.content.length>0&&x.content.every(p=>/^─+$/.test(p.text.trim()||'x')||!p.text.trim());
const isRule=x=>/^─+$/.test(words(x).trim())&&words(x).trim().length>=10;
// What a popup shows: its content rows' words, in order (the top and bottom lines left out).
const rowsOf=r=>{const f=frame(r);return f.slice(1,f.length-1).map(words);};
const out={};
// (1) A long question with choices: words, one empty row, the line, then the choices.
{const b=editor(),r=draw(b),f=frame(r),body=f.slice(1,f.length-1);
 out.rows=body.map(words);
 const at=body.findIndex(x=>/^▸ 1  /.test(words(x)));
 out.at=at;
 out.rule_width=isRule(body[at-1])?words(body[at-1]).length:0;out.inner=r.inner;
 // The line is the frame's own color: the same foreground and ground as the top line's rule.
 const top=f[0].row.find(p=>/^─+$/.test(p.text)),rule=body[at-1].content.find(p=>/^─+$/.test(p.text));
 out.line=[rule.foreground,rule.background,rule.dim,rule.bold];out.frame_line=[top.foreground,top.background];out.popupLine=F.popupLine;
 // The numbers: the question's rose (the `◑` mark's color), bold, two cells before the words, on every choice's row.
 const mark=f[0].row.find(p=>p.text.includes('◑'));out.mark=mark&&mark.foreground;
 out.numbers=body.slice(at).flatMap(x=>x.content.filter(p=>/^\d+  $/.test(p.text)).map(p=>[p.text,p.foreground,p.bold,p.dim===true,p.background]));
 // The chosen row stays: `▸` bold in the harness color, the chosen ground across it, its words bold.
 const lit=body.filter(x=>x.content.some(p=>p.background===F.chosen));
 out.chosen=lit.map(words);out.chosen_mark=lit[0].content.some(p=>p.text==='▸'&&p.bold===true);
 out.chosen_words_bold=lit[0].content.filter(p=>/아래 두 줄/.test(p.text)).every(p=>p.bold===true);
 // The other choices read dim, words not bold, numbers still the rose and bold.
 const other=body.find(x=>/^ ?2  /.test(words(x))||/^2  /.test(words(x).replace(/^ +/,'')));
 out.other_words=other.content.filter(p=>/넓힌/.test(p.text)).map(p=>[p.bold===true,p.foreground]);out.floor=F.popupFloor;
 out.other_bg=other.content.every(p=>p.background===F.popup);}
// (2) Before the first ↑↓ (not choosing) the rows read the same way.
{const b=editor();key(b,'\x1b[B');key(b,'\x1b[A');const r=draw(b),body=frame(r).slice(1,-1);
 out.after_arrows=body.map(words).findIndex(w=>/^▸ 1  /.test(w))-body.map(words).findIndex(w=>/^─+$/.test(w.trim()));}
// (3) A question answered in words: no line, no empty row before anything. Tab to it, then to a short one.
{const b=editor();key(b,'\t');const r=draw(b),body=frame(r).slice(1,-1);
 out.text_rows=body.map(words);out.text_rules=body.filter(isRule).length;
 key(b,'\t');const s=draw(b),sb=frame(s).slice(1,-1),sw=sb.map(words),i=sw.findIndex(w=>/^▸ 1  /.test(w));
 out.second=[sw,i,isRule(sb[i-1]),sw[i-2]];
 key(b,'\t');const d=draw(b),db=frame(d).slice(1,-1),dw=db.map(words),j=dw.findIndex(w=>/approve|^1  /.test(w));
 out.decision=[dw,j,j>0&&isRule(db[j-1])];}
// (4) Short of rows: the empty row goes first, the line stays, choices and keys never pushed out.
{const b=editor(),shown=[];
 for(let budget=40;budget>=5;budget--){
  const rows=b.detail({budget,columns:72}),text=rows.map(row=>row.map(p=>p.text).join(''));
  shown.push({budget,length:rows.length,blank:rows.some((row,i)=>i>0&&row.length===0),rule:text.some(t=>/^─{10,}$/.test(t)),
    numbers:text.filter(t=>/^ {0,2}(▸ )?\d  /.test(t)).length,more:text.some(t=>/^\+\d+ ·/.test(t)),
    keys:/Tab next/.test(text.at(-1)||''),
    ahead:(()=>{const i=text.findIndex(t=>/^─{10,}$/.test(t));return i>0?[rows[i-1].length===0,text[i-1]]:null;})()});}
 out.shown=shown;
 // Read to the end with PgDn: the line before the first choice, the choices and the keys on the last page.
 const e=editor();let last;for(let n=0;n<30;n++){key(e,'\x1b[6~');last=e.detail({budget:10,columns:72}).map(row=>row.map(p=>p.text).join(''));}
 out.end=last;}
// (5) No hue (mono, eight colors, NO_COLOR): the numbers' weight and the line tell; no ground; the line is dim in a bright frame.
out.plain=[{configuration:{theme:'mono'}},{colors:8},{noColor:true}].map(o=>{
 const sty=o.noColor?Object.fromEntries(Object.entries(styles).map(([k,s])=>[k,{...s,foreground:null,bold:false,dim:false}])):styles;
 const p=draw(editor(items,sty),o),f=frame(p),body=f.slice(1,-1),at=body.findIndex(x=>/^▸ 1  /.test(words(x)));
 const rule=body[at-1].content.find(q=>/^─+$/.test(q.text));
 return {rule:isRule(body[at-1]),blank:words(body[at-2]).trim()==='',rule_dim:rule.dim===true,rule_fg:rule.foreground??null,
  numbers:body.slice(at).flatMap(x=>x.content.filter(q=>/^\d+  $/.test(q.text)).map(q=>q.bold===true)),
  grounds:p.rows.flat().filter(q=>Number.isInteger(q.background)).length};});
// (6) A light face: the line is that face's frame line.
{const r=draw(editor(),{background:'light'}),f=frame(r),body=f.slice(1,-1),at=body.findIndex(x=>/^▸ 1  /.test(words(x)));
 const top=f[0].row.find(p=>/^─+$/.test(p.text)),rule=body[at-1].content.find(p=>/^─+$/.test(p.text));
 out.light=[rule.foreground,top.foreground,L.popupLine,rule.background,L.popup];}
// (7) 24-bit color: the line takes the frame's 24-bit value.
{const r=draw(editor(),{colors:16777216}),f=frame(r),body=f.slice(1,-1),at=body.findIndex(x=>/^▸ 1  /.test(words(x)));
 const top=f[0].row.find(p=>/^─+$/.test(p.text)),rule=body[at-1].content.find(p=>/^─+$/.test(p.text));
 out.truecolor=[rule.foreground,top.foreground,F.rgb.popupLine];}
console.log(JSON.stringify(out));
"""


class QuestionSplitTest(unittest.TestCase):
    _out = None

    def run_script(self):
        if QuestionSplitTest._out is None:
            result = subprocess.run(["node", "-e", SCRIPT, str(SHELL), json.dumps(presented(), ensure_ascii=False),
                                     json.dumps(styles())], capture_output=True, text=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            QuestionSplitTest._out = json.loads(result.stdout.strip().splitlines()[-1])
        return QuestionSplitTest._out

    # -- the backend's rows -----------------------------------------------------------------------------------

    def test_the_backend_sends_each_number_in_the_questions_rose_with_two_cells_after_it(self):
        view = presented()[0]["questions"][0]["display"]
        numbers = [line[0] for line in view["options"] if line[0]["text"].strip()]
        self.assertEqual([(n["text"], n["key"]) for n in numbers], [("1  ", "g_blocked"), ("2  ", "g_blocked"), ("3  ", "g_blocked")])
        # A wrapped choice's next rows are indented by the same three cells, in no color of their own.
        wrapped = [line[0] for line in view["options"] if not line[0]["text"].strip()]
        self.assertTrue(wrapped and all(w["text"] == "   " and w["key"] == "dim" for w in wrapped), wrapped)
        # The panel's one row of choices: its numbers wear the rose too (its single space is kept: lines and places stay).
        brief = presented()[2]["questions"][0]["display"]["brief"]
        parts = [part for line in brief for part in line]
        self.assertEqual([(p["text"], p["key"]) for p in parts if p["text"].strip() in ("1", "2")],
                         [("1 ", "g_blocked"), ("2 ", "g_blocked")])

    # -- the popup ----------------------------------------------------------------------------------------------

    def test_a_question_with_choices_ends_its_words_then_one_empty_row_then_the_line_then_the_choices(self):
        out = self.run_script()
        at, rows = out["at"], out["rows"]
        self.assertGreater(at, 4, rows)
        self.assertTrue(set(rows[at - 1].strip()) == {"─"} and len(rows[at - 1].strip()) == out["inner"], rows)   # the line, the popup's width
        self.assertEqual(rows[at - 2].strip(), "", rows)                                                                  # one empty row above it
        self.assertEqual(rows[at - 3], "고른 안만 따로 구현하고, 나머지는 시안으로 남깁니다.", rows)               # the question's last line
        self.assertTrue(rows[at - 4].strip(), rows)                                                               # only one empty row
        self.assertTrue(rows[at + 1].lstrip().startswith("2  넓힌 옆 띠 (가)"), rows)
        self.assertEqual(out["after_arrows"], 1, out)

    def test_the_line_is_the_popup_frames_own_color_and_the_numbers_are_the_questions_rose_and_bold(self):
        out = self.run_script()
        self.assertEqual(out["line"][:2], out["frame_line"], out)
        self.assertEqual(out["line"][0], out["popupLine"], out)
        self.assertFalse(out["line"][2] or out["line"][3], out)
        self.assertIsNotNone(out["mark"])
        # Three numbers, each the `◑` mark's rose and bold, on the chosen row's ground and the others'.
        self.assertEqual([n[0] for n in out["numbers"]], ["1  ", "2  ", "3  "], out["numbers"])
        for text, foreground, bold, dim, _background in out["numbers"]:
            self.assertEqual((foreground, bold, dim), (out["mark"], True, False), text)
        self.assertNotEqual(out["numbers"][0][4], out["numbers"][1][4], "the chosen row keeps its own ground")

    def test_the_chosen_row_keeps_its_mark_ground_and_weight_and_the_others_read_dim(self):
        out = self.run_script()
        self.assertEqual(len(out["chosen"]), 1, out["chosen"])
        self.assertTrue(out["chosen"][0].startswith("▸ 1  아래 두 줄 (다)"), out["chosen"])
        self.assertTrue(out["chosen_mark"] and out["chosen_words_bold"], out)
        self.assertTrue(out["other_words"] and all(bold is False and foreground == out["floor"] for bold, foreground in out["other_words"]), out["other_words"])
        self.assertTrue(out["other_bg"], out)

    def test_a_question_answered_in_words_has_neither_the_line_nor_the_empty_row(self):
        out = self.run_script()
        self.assertEqual(out["text_rules"], 0, out["text_rows"])
        self.assertFalse(any(row.startswith("─") for row in out["text_rows"]), out["text_rows"])
        self.assertEqual([r for r in out["text_rows"] if not r.strip()], [], out["text_rows"])

    def test_every_question_of_a_pair_is_parted_the_same_way_and_a_decision_too(self):
        out = self.run_script()
        rows, at, ruled, above = out["second"]
        self.assertTrue(ruled and above.strip() == "", out["second"])
        self.assertEqual(rows[at - 3], "색을 지금 그대로 둘까요, 한 단계 밝게 할까요?", rows)
        rows, at, ruled = out["decision"]
        self.assertTrue(ruled, out["decision"])
        self.assertEqual(rows[at - 2].strip(), "", rows)

    def test_short_of_rows_the_empty_row_goes_first_and_the_line_and_the_choices_and_keys_stay(self):
        out = self.run_script()
        shown = out["shown"]
        # A roomy popup has the empty row and the line; the empty row is never there without the line.
        self.assertTrue(shown[0]["blank"] and shown[0]["rule"], shown[0])
        self.assertTrue(all(s["rule"] for s in shown if s["blank"]), shown)
        # 11-22: short of rows the question's words page (`+N · more …`); the choices never leave. What gives way
        # first is the key hints, then the empty row, then the line, then the words themselves (the head says they page).
        self.assertTrue(all(s["numbers"] == 3 for s in shown), [s for s in shown if s["numbers"] != 3])
        self.assertTrue(all(not (s["keys"] and not s["blank"]) for s in shown), shown)
        self.assertTrue(all(not (s["blank"] and not s["rule"]) for s in shown), shown)
        keyless = [s for s in shown if not s["keys"] and s["budget"] >= 7]
        self.assertTrue(keyless and all(s["more"] for s in keyless), keyless)
        self.assertTrue(any(s["more"] and s["keys"] and s["blank"] and s["rule"] for s in shown), shown)
        self.assertTrue(any(s["rule"] and not s["blank"] and not s["keys"] for s in shown), shown)
        self.assertTrue(any(not s["rule"] and not s["blank"] and s["more"] for s in shown), shown)
        self.assertTrue(any(s["more"] for s in shown))
        # Where the question is read page by page, the end of it is the line, the choices and the keys.
        end = out["end"]
        self.assertTrue(any(set(t) == {"─"} for t in end), end)
        self.assertIn("Tab next", end[-1], end)
        self.assertTrue(any("▸" in t and "1  " in t for t in end) or any(t.lstrip().startswith("3  ") for t in end), end)

    def test_where_there_is_no_color_the_numbers_weight_and_the_line_tell_the_two_parts_apart(self):
        out = self.run_script()
        for plain in out["plain"]:
            self.assertTrue(plain["rule"] and plain["blank"], plain)
            self.assertEqual(plain["numbers"], [True, True, True], plain)
            self.assertEqual(plain["grounds"], 0, plain)
        mono, eight, none = out["plain"]
        self.assertTrue(mono["rule_dim"] and none["rule_dim"], out["plain"])        # a bright frame: the line is the dim one

    def test_the_light_face_and_the_24_bit_color_take_the_frames_line_value(self):
        out = self.run_script()
        rule, top, expected, ground, popup = out["light"]
        self.assertEqual((rule, ground), (top, popup), out["light"])
        self.assertEqual(rule, expected, out["light"])
        self.assertEqual(out["truecolor"][0], out["truecolor"][2], out["truecolor"])
        self.assertEqual(out["truecolor"][0], out["truecolor"][1], out["truecolor"])


if __name__ == "__main__":
    unittest.main()
