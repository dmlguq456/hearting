"""11-22 ("애초에 선지가 선택가능하게 장치를 만들어야지.." / "선지가 선택이 안되고 그냥 글에 있는 경우도 있다는거야"): a question with
choices always shows its choice rows.

The popup's question view used to page the question's words and its choices as one block: a long question left no choice
row on its first page (unfocused), and a focused one pushed all but the highlighted choice out. Now only the words page
(`+N · more …`, PgUp/PgDn); the choices and the answer row stand. Short of rows the key hints go first, then the empty
row and the thin line, then the words (down to one row and the `more` line, never gone: PgUp/PgDn read them on), and
last the choices slide to keep the highlighted one in sight with a line saying there are more. These run the real
editor with synthetic values.
"""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
from pathlib import Path
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]
SHELL = ROOT / "utilities/session-shell"

PRELUDE = r"""
const assert=require('node:assert/strict'),create=require(process.argv[1]+'/block-input.cjs');
const measure=s=>[...s].reduce((n,c)=>n+(/\p{Mark}/u.test(c)?0:c.codePointAt(0)>=0x2e80?2:1),0);
function setup({lines=1,choices=3,free=true,focus=true,columns=60}={}){
  let sent;
  const b=create({cellWidth:measure,changed(){},action(){},answer:async x=>(sent=x,{state:'submitted'})});
  const body=Array.from({length:lines},(_,i)=>[{text:'body line '+(i+1)}]);
  const list=Array.from({length:choices},(_,i)=>({value:'v'+(i+1),label:'Choice '+(i+1)}));
  b.update([{id:'synthetic',revision:'one',answerable:true,questions:[{id:'q',question:'Synthetic',free_text:free,choices:list,
    display:{body}}]}],'',{});
  if(focus)b.enter();
  return {b,rows:(budget=16)=>b.detail({columns,budget}).map(r=>r.map(p=>p.text).join('')),keys:k=>b.input(Buffer.from(k)),
    async send(k='\r'){b.input(Buffer.from(k));await new Promise(r=>setImmediate(r));return sent;}};
}
const seen=rows=>rows.filter(r=>/Choice \d/.test(r)).map(r=>Number(/Choice (\d)/.exec(r)[1]));
"""


class QuestionChoicesTest(unittest.TestCase):
    def node(self, body):
        result = subprocess.run(["node", "-e", PRELUDE + "\n(async()=>{" + body + "})().catch(e=>{console.error(e);process.exit(1)});",
                                 str(SHELL)], capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout

    def test_the_choices_are_always_in_sight_across_body_lengths_choices_limits_and_focus(self):
        out = self.node(r"""
  let checked=0;
  for(const free of [true,false])for(const focus of [true,false])for(const lines of [1,5,17,40])for(const choices of [2,4,9])for(const budget of [6,10,16,24]){
    const x=setup({lines,choices,free,focus}),rows=x.rows(budget),shown=seen(rows),tag=JSON.stringify({free,focus,lines,choices,budget});
    assert(rows.length<=budget,tag+' rows');
    assert(shown.length>=1,tag+' at least one choice row\n'+rows.join('\n'));
    assert.deepEqual(shown,[...shown].sort((a,b)=>a-b),tag+' in order');
    if(budget>=choices+3+(free?1:0))assert.equal(shown.length,choices,tag+' every choice that fits beside a row of words and its more line\n'+rows.join('\n'));
    if(free&&budget>=3)assert(rows.some(r=>r.startsWith('› ')),tag+' the answer row stands\n'+rows.join('\n'));
    checked++;
  }
  console.log('CASES '+checked);
""")
        self.assertIn("CASES 192", out)

    def test_the_repro_17_words_3_choices_16_rows_before_and_after(self):
        # Unfocused: the choices show; focused: all three show, not only the highlighted one.
        self.node(r"""
  for(const focus of [false,true]){
    const x=setup({lines:17,choices:3,focus}),rows=x.rows(16);
    assert.deepEqual(seen(rows),[1,2,3],rows.join('\n'));
    assert(rows.some(r=>r.includes('more')),'the words page: '+rows.join('\n'));
    assert(rows.some(r=>r.includes('body line 1')));
  }
""")

    def test_the_words_page_with_pgdn_pgup_and_the_choices_stay(self):
        self.node(r"""
  const x=setup({lines:40,choices:4}),first=x.rows(12);
  assert(first.includes('body line 1'));
  x.keys('\x1b[6~');const next=x.rows(12);
  assert(!next.includes('body line 1'),next.join('\n'));assert.deepEqual(seen(next),[1,2,3,4]);
  x.keys('\x1b[5~');assert(x.rows(12).includes('body line 1'));
  // The wheel reads the words too; ↑↓ choose and the highlighted choice is marked, the words stay.
  x.b.wheel(3);assert(!x.rows(12).includes('body line 1'));
  x.keys('\x1b[B');assert(x.rows(12).some(r=>r.includes('▶ 2')),x.rows(12).join('\n'));assert.deepEqual(seen(x.rows(12)),[1,2,3,4]);
  assert.deepEqual((await x.send())?.answers.q,['v2']);
""")

    def test_short_of_rows_the_hints_go_then_the_rule_then_the_words_then_the_choices_slide(self):
        self.node(r"""
  // Plenty: words whole, the empty row and the thin line, the choices, the answer row and its key hints.
  let x=setup({lines:2,choices:3}),rows=x.rows(24);
  assert(rows.some(r=>r.includes('─')));assert(rows.some(r=>r.includes('Esc')));
  // Tight: the key hints go first (10 rows hold hints, the rule and a body row with its `more` line).
  rows=setup({lines:17,choices:3}).rows(10);assert(rows.some(r=>r.includes('Esc')),rows.join('\n'));
  rows=setup({lines:17,choices:3}).rows(9);assert(rows.some(r=>r.includes('─'))&&!rows.some(r=>r.includes('Esc')),rows.join('\n'));
  assert(rows.some(r=>r===''),'the empty row is still there at 9');
  // Tighter: the empty row goes, then the thin line.
  rows=setup({lines:17,choices:3}).rows(8);assert(rows.some(r=>r.includes('─'))&&!rows.some(r=>r===''),rows.join('\n'));
  rows=setup({lines:17,choices:3}).rows(7);assert(!rows.some(r=>r.includes('─'))&&seen(rows).length===3&&rows.some(r=>r.includes('body line 1')),rows.join('\n'));
  // The words go down to one row and the `more` line before a choice is given up: the choices slide around the
  // highlighted one and say there are more; the words stay.
  rows=setup({lines:17,choices:3}).rows(6);assert(rows.some(r=>r.includes('body line 1'))&&rows.some(r=>r.includes('+16 · more'))&&rows.some(r=>r.includes('more choices')),rows.join('\n'));
  assert(rows.some(r=>r.includes('▶ 1')),rows.join('\n'));
  // Tighter: one row of words with its `more` line and one choice; then one row of words with the head saying it pages
  // and one choice; then (one row left) the choice alone, the head promising no words.
  rows=setup({lines:17,choices:3}).rows(5);assert(rows.some(r=>r.includes('+16 · more'))&&seen(rows).length===1,rows.join('\n'));
  rows=setup({lines:17,choices:3}).rows(4);assert(rows.some(r=>r.includes('body line 1'))&&rows[0].includes('+16')&&seen(rows).length===1,rows.join('\n'));
  rows=setup({lines:17,choices:3}).rows(3);assert(seen(rows).length===1&&!rows.some(r=>r.includes('body line'))&&!rows[0].includes('PgUp'),rows.join('\n'));
  // Not enough for the choices: they slide around the highlighted one and say there are more, the words beside them.
  x=setup({lines:17,choices:9});x.keys('\x1b[B'.repeat(6));rows=x.rows(8);
  assert(rows.some(r=>r.includes('▶ 7')),rows.join('\n'));assert(rows.some(r=>r.includes('more choices')),rows.join('\n'));assert(seen(rows).length>=1);
  assert(rows.some(r=>r.includes('body line 1')),'the words stay beside the sliding choices\n'+rows.join('\n'));
  x=setup({lines:17,choices:9,focus:false});rows=x.rows(8);assert(seen(rows)[0]===1&&rows.some(r=>r.includes('more choices')),rows.join('\n'));
""")

    def test_the_repro_8_choices_in_10_rows_and_4_choices_in_6_rows_keep_the_words_and_they_page(self):
        self.node(r"""
  let x=setup({lines:40,choices:8}),rows=x.rows(10);
  assert(rows.includes('body line 1')&&rows.some(r=>r.includes('more · PgUp/PgDn to read 1/40')),rows.join('\n'));
  assert.deepEqual(seen(rows),[1,2,3,4,5],rows.join('\n'));assert(rows.some(r=>r.includes('+3 · more choices')));
  x.keys('\x1b[6~');assert(!x.rows(10).includes('body line 1'),x.rows(10).join('\n'));
  x=setup({lines:17,choices:4});rows=x.rows(6);
  assert(rows.includes('body line 1')&&rows.some(r=>r.includes('+16 · more')),rows.join('\n'));assert(seen(rows).length===1&&rows.some(r=>r.includes('+3 · more choices')),rows.join('\n'));
""")

    def test_whenever_the_words_show_the_page_keys_read_them_to_the_last_row(self):
        # Body 1·5·17·40·200 rows × 1·2·4·8 choices × limits 3..30 × focus none / choosing / typing × an answer row or none.
        out = self.node(r"""
  let reading=0,cells=0;
  for(const lines of [1,5,17,40,200])for(const choices of [1,2,4,8])for(const budget of Array.from({length:28},(_,i)=>i+3))
  for(const free of [true,false])for(const mode of ['none','choosing','typing']){
    if(mode==='typing'&&!free)continue;
    const x=setup({lines,choices,free,focus:mode!=='none'});if(mode==='typing')x.keys('x');
    const rows=x.rows(budget),tag=JSON.stringify({lines,choices,budget,free,mode});cells++;
    assert(rows.length<=budget,tag+' rows');
    assert(seen(rows).length>=1,tag+' a choice row stands\n'+rows.join('\n'));
    const shown=rows.filter(r=>/^body line \d+$/.test(r)).length;
    // The words are there whenever the limit holds the head, a row of words and a choice row (and the answer row).
    if(budget>=3+(free?1:0))assert(shown>=1,tag+' the words stay\n'+rows.join('\n'));
    // No screen promises more words (the `more` line, the head's `+N`) while none is shown.
    if(shown===0)assert(!rows.some(r=>/\+\d+ · more|PgUp|\+\d+ ↑↓/.test(r)&&!/more choices/.test(r)),tag+' no promise of words that are not there\n'+rows.join('\n'));
    if(mode==='none'||shown===0||shown>=lines)continue;
    // The page keys read the words on, to the last row, and back to the first.
    let last=x.rows(budget),steps=0;
    while(!last.includes('body line '+lines)&&steps++<400){x.keys('\x1b[6~');const next=x.rows(budget);assert.notDeepEqual(next,last,tag+' PgDn reads on\n'+last.join('\n'));last=next;assert(seen(next).length>=1,tag+' choices stay');}
    assert(last.includes('body line '+lines),tag+' the last row is reached\n'+last.join('\n'));
    for(let i=0;i<400&&!x.rows(budget).includes('body line 1');i++)x.keys('\x1b[5~');
    assert(x.rows(budget).includes('body line 1'),tag+' back to the first row');
    reading++;
  }
  console.log('CELLS '+cells+' READ '+reading);
""")
        self.assertIn("CELLS 2800", out)


    def test_a_question_without_choices_or_a_decision_pages_as_before(self):
        self.node(r"""
  const x=setup({lines:40,choices:0});const rows=x.rows(10);
  assert(rows.some(r=>r.includes('more')));assert(!rows.some(r=>r.includes('more choices')));assert(rows.includes('body line 1'));
""")


if __name__ == "__main__":
    unittest.main()
