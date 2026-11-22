"""11-22 ("질문에 다른 사항을 입력할때 텍스트 수정이 중간으로 가서 하는게 안되네? 화살표로"): the typed answer has a place.

The shell's question input (the panel's QUESTIONS box once focused, and the popup, one editor) keeps a place inside the
typed text: ←→ move one character as the person sees it, Home/End (also Ctrl+A/E, `ESC [ H/F`, SS3, `ESC [ 1~/4~`) go to
its ends, typed characters and a paste go in at the place, Backspace removes before it and Delete (`ESC [ 3~`) at it.
The place is a screen value: the sent answer, the 4,096-byte limit, the secret bullets, the keys that had a meaning
(←→ with no text, ↑ ↓ Tab, numbers, Enter, Esc) are as before. These run the real editor and the real popup layout with
synthetic values; no terminal, no account, no session of the person's.
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
const assert=require('node:assert/strict'),create=require(process.argv[1]+'/block-input.cjs'),view=require(process.argv[1]+'/panel-view.cjs');
const measure=s=>[...s].reduce((n,c)=>n+(/\p{Mark}/u.test(c)?0:c.codePointAt(0)>=0x2e80?2:1),0);
function setup(extra={},choices=[{value:'v1',label:'First'},{value:'v2',label:'Second'}]){
  let sent,sections=[];
  const b=create({cellWidth:measure,changed(){},action(){},section:d=>sections.push(d),answer:async x=>(sent=x,{state:'submitted'})});
  b.update([{id:'synthetic',revision:'one',answerable:true,questions:[{id:'q',question:'Synthetic question',free_text:true,choices,...extra}]}],'',{});b.enter();
  return {b,sections,keys:k=>b.input(Buffer.from(k)),
    rows:(columns=60,budget=24)=>b.detail({columns,budget}),
    async send(k='\r'){b.input(Buffer.from(k));await new Promise(r=>setImmediate(r));return sent;}};
}
const plain=r=>r.map(p=>p.text).join('');
// The text before the place on its row (the terminal cursor stands right after it), and the whole row.
function place(x,columns=60,budget=24){const rows=x.rows(columns,budget),at=rows.findIndex(r=>r.some(p=>p.caret));
  assert(at>=0,'a row holds the place');const row=rows[at],i=row.findIndex(p=>p.caret);
  return {front:row.slice(0,i+1).map(p=>p.text).join(''),row:plain(row),rows};}
const LEFT='\x1b[D',RIGHT='\x1b[C',HOME='\x1b[H',END='\x1b[F',DEL='\x1b[3~';
"""


class QuestionCaretTest(unittest.TestCase):
    def node(self, body):
        result = subprocess.run(["node", "-e", PRELUDE + "\n(async()=>{" + body + "})().catch(e=>{console.error(e);process.exit(1)});",
                                 str(SHELL)], capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout

    def test_typing_backspace_and_delete_work_inside_the_text(self):
        self.node(r"""
  let x=setup();x.keys('abcd'+LEFT+LEFT+'X');
  assert.equal(place(x).front,'› abX');assert.equal(place(x).row,'› abXcd');
  assert.deepEqual((await x.send())?.answers.q,['abXcd']);
  x=setup();x.keys('abcd'+LEFT+LEFT+'\x7f');assert.equal(place(x).row,'› acd');assert.equal(place(x).front,'› a');
  assert.deepEqual((await x.send())?.answers.q,['acd']);
  x=setup();x.keys('abcd'+LEFT+LEFT+DEL);assert.equal(place(x).row,'› abd');assert.equal(place(x).front,'› ab');
  assert.deepEqual((await x.send())?.answers.q,['abd']);
  // Delete at the end and Backspace at the start take nothing.
  x=setup();x.keys('ab'+DEL);assert.equal(place(x).row,'› ab');x.keys(HOME+'\x7f');assert.equal(place(x).row,'› ab');
  assert.equal(place(x).front,'› ');
  // The place stays where the edit left it: three characters in, then one before it.
  x=setup();x.keys('abcdef'+HOME+RIGHT+RIGHT+RIGHT+'\x7f\x7f'+'Z');assert.equal(place(x).row,'› aZdef');
  // A held Backspace walks back to nothing and stops.
  x=setup();x.keys('ab'+'\x7f'.repeat(5));assert.equal(place(x).row,'› ');
""")

    def test_home_end_and_ctrl_a_e_in_every_terminal_form(self):
        self.node(r"""
  for(const start of ['\x01','\x1b[H','\x1bOH','\x1b[1~','\x1b[7~'])for(const end of ['\x05','\x1b[F','\x1bOF','\x1b[4~','\x1b[8~']){
    const x=setup();x.keys('hello'+start);assert.equal(place(x).front,'› ',JSON.stringify(start));
    x.keys('X');assert.equal(place(x).row,'› Xhello');x.keys(end+'Y');assert.equal(place(x).row,'› XhelloY');assert.equal(place(x).front,'› XhelloY');
  }
  // SS3 arrows move too, and an arrow past either end stays at the end.
  const x=setup();x.keys('abc\x1bOD\x1bOD\x1bOD\x1bOD');assert.equal(place(x).front,'› ');x.keys('\x1bOC\x1bOC\x1bOC\x1bOC\x1bOC');assert.equal(place(x).front,'› abc');
""")

    def test_one_step_is_one_character_the_person_sees(self):
        self.node(r"""
  // A Hangul syllable, a wide character, a base with its combining mark, and a decomposed syllable are one step each.
  let x=setup();x.keys('가나다'+LEFT);assert.equal(place(x).front,'› 가나');
  x.keys(LEFT);assert.equal(place(x).front,'› 가');x.keys('X');assert.equal(place(x).row,'› 가X나다');
  x=setup();x.keys('漢字語'+LEFT+LEFT+'\x7f');assert.equal(place(x).row,'› 字語');
  x=setup();x.keys('aéb'+LEFT);assert.equal(place(x).front,'› aé');x.keys(LEFT);assert.equal(place(x).front,'› a');
  x.keys(RIGHT+RIGHT);assert.equal(place(x).front,'› aéb');
  x=setup();x.keys('aéb'+LEFT+'\x7f');assert.equal(place(x).row,'› ab');   // Backspace removes the whole character
  x=setup();x.keys('aéb'+LEFT+LEFT+DEL);assert.equal(place(x).row,'› ab');  // Delete too
  x=setup();x.keys('각z'+LEFT);assert.equal(place(x).front,'› 각');x.keys(LEFT+'q');assert.equal(place(x).row,'› q각z');
  x=setup();x.keys('a\ud83d\udc4d\ud83c\udffdb'+LEFT+LEFT+DEL);assert.equal(place(x).row,'› ab');
  x=setup();x.keys('a\ud83d\udc4d\ud83c\udffdb'+LEFT+'\x7f');assert.equal(place(x).row,'› ab');
""")

    def test_the_place_is_drawn_on_its_wrapped_row_and_the_arrow_crosses_rows(self):
        self.node(r"""
  for(const columns of [26,40,55,96]){
    const text='한글 답변 abc '.repeat(12)+'끝',x=setup();x.keys(text);
    const full=place(x,columns,40);assert(full.row.endsWith('끝'));
    const rows=full.rows.filter(r=>r.some(p=>p.answerRow)),words=rows.map(plain);assert(rows.length>1);
    // Home: the place is on the first row, at its first cell.
    x.keys(HOME);let p=place(x,columns,40);assert.equal(p.front,'› ');assert.equal(p.rows.filter(r=>r.some(q=>q.caret)).length,1);
    assert.deepEqual(p.rows.filter(r=>r.some(q=>q.answerRow)).map(plain),words);
    // Walk right to the last character of the first row, then once more: the first cell of the second row.
    const lead=[...words[0].slice(2)].length;x.keys(RIGHT.repeat(lead-1));p=place(x,columns,40);assert.equal(p.row,words[0]);assert.equal(p.front,words[0].slice(0,-1));
    x.keys(RIGHT);p=place(x,columns,40);assert.equal(p.row,words[1]);assert.equal(p.front,'  ');
    // Two more: the second cell's place is two characters into that row.
    x.keys(RIGHT+RIGHT);p=place(x,columns,40);assert.equal(p.front,words[1].slice(0,2)+[...words[1].slice(2)].slice(0,2).join(''));
    // Back across the boundary and one past the end of the whole text.
    x.keys(END);p=place(x,columns,40);assert.equal(p.row,words.at(-1));assert.equal(p.front,words.at(-1));
    assert(measure(p.front)<columns);
  }
""")

    def test_a_short_budget_keeps_the_row_with_the_place(self):
        self.node(r"""
  const text='한글 답변 '.repeat(30)+'끝',x=setup();x.keys(text);
  for(const columns of [26,44,96])for(const budget of [30,6,3,2]){
    x.keys(END);let p=place(x,columns,budget);assert(p.row.endsWith('끝'),'the last row holds the place at the end');
    x.keys(HOME);p=place(x,columns,budget);assert.equal(p.front,'› ');assert(p.row.startsWith('› 한글'),'the first row holds the place at the start');
    assert(p.rows.length<=budget);
    x.keys(RIGHT.repeat(60));p=place(x,columns,budget);assert(p.front.length>2||columns<30);assert(p.rows.length<=budget);
  }
""")

    def test_a_secret_answer_is_bullets_and_the_place_moves_over_them(self):
        self.node(r"""
  const x=setup({secret:true});x.keys('private'+LEFT+LEFT+LEFT);
  const p=place(x);assert.equal(p.row,'› •••••••');assert.equal(p.front,'› ••••');assert(!p.rows.map(plain).join('\n').includes('private'));
  x.keys('Z');assert.equal(place(x).row,'› ••••••••');x.keys(DEL);x.keys('\x7f');
  assert.deepEqual((await x.send())?.answers.q,['privte']);
""")

    def test_a_paste_goes_in_at_the_place_and_the_limit_refuses_it_there(self):
        self.node(r"""
  let x=setup();x.keys('abcd'+LEFT+LEFT+'\x1b[200~가나\x1b[201~');assert.equal(place(x).row,'› ab가나cd');assert.equal(place(x).front,'› ab가나');
  x.keys('Y');assert.equal(place(x).row,'› ab가나Ycd');
  assert.deepEqual((await x.send())?.answers.q,['ab가나Ycd']);
  // A paste split across reads, ending mid-text.
  x=setup();x.keys('xyz'+HOME+'\x1b[20');x.keys('0~AB\x1b[201');x.keys('~');assert.equal(place(x).row,'› ABxyz');
  // The 4,096-byte limit: an insert in the middle is refused as one at the end is, and the text stays.
  x=setup();x.keys('a'.repeat(4096)+LEFT+LEFT+'b');
  assert(x.rows().some(r=>plain(r).includes('answer is full')));assert(!place(x).rows.filter(r=>r.some(p=>p.answerRow)).map(plain).join('').includes('b'));
  x.keys('\x1b[200~'+'가'+'\x1b[201~');assert(!place(x).row.includes('가'));
  assert.deepEqual((await x.send())?.answers.q,['a'.repeat(4096)]);
  // A multi-byte insert that would cross the limit by one byte is refused whole.
  x=setup();x.keys('a'.repeat(4094)+LEFT+'가');assert(!place(x).row.includes('가'));x.keys('b');assert(place(x).row.includes('b'));
""")

    def test_keys_that_had_a_meaning_keep_it(self):
        self.node(r"""
  // No text: ←→ still change sections (also with Home/End/Delete doing nothing).
  let x=setup();x.keys(LEFT+RIGHT+'\x1bOD\x1bOC'+HOME+END+DEL+'\x01\x05');assert.deepEqual(x.sections,[-1,1,-1,1]);

  // Typing with no text yet: ←→ are still not sections (as before), and nothing is moved.
  x=setup();x.b.write();x.keys(LEFT+RIGHT);assert.deepEqual(x.sections,[]);
  // With text the arrows stay inside it; they are not sections.
  x=setup();x.keys('ab'+LEFT+RIGHT+LEFT);assert.deepEqual(x.sections,[]);assert.equal(place(x).front,'› a');
  // ↑ leaves the answer row for the choices and keeps the text; coming back down the place is the end.
  x.keys('\x1b[A');assert(!x.b.typing);x.keys('\x1b[B'.repeat(3));assert(x.b.typing);assert.equal(place(x).front,'› ab');
  x.keys('2');assert.deepEqual((await x.send())?.answers.q,['v2','ab2']);   // ↓ marked the second choice on its way
  // A number picks a choice before writing; Enter sends the highlighted one; digits are text once writing.
  x=setup();assert.deepEqual((await x.send('2\r'))?.answers.q,['v2']);
  x=setup();x.keys('ab'+LEFT+'1');assert.equal(place(x).row,'› a1b');
  // Tab and back: the place is the text's end again (the text itself is kept).
  x=setup();x.b.update([{id:'synthetic',revision:'one',answerable:true,questions:['q','q2'].map(id=>({id,question:id,free_text:true,choices:['one','two']}))}],'',{});
  x.keys('abc'+HOME+'\t\t\t');   // q2, the empty slot after the last question, q againassert.equal(place(x).front,'› abc');assert.equal(place(x).row,'› abc');
  // Closing and opening the popup again: the text is kept, the place is its end.
  x=setup();x.keys('abc'+HOME+'\x1d');x.b.enter();x.b.write();assert.equal(place(x).front,'› abc');
  // The sent answer is the text alone, as before (the place is not in it).
  x=setup();x.keys('abc'+HOME+'Z');assert.deepEqual((await x.send())?.answers.q,['Zabc']);
""")

    def test_a_choiceless_free_text_question_and_a_decision_are_untouched(self):
        self.node(r"""
  const x=setup({},[]);x.keys('ab'+LEFT+'X');assert.equal(place(x).row,'› aXb');assert.deepEqual((await x.send())?.answers.q,['aXb']);
  // A choice-only question has no answer row; the arrows there change sections as before.
  const y=setup({free_text:false});y.keys(LEFT+RIGHT);assert.deepEqual(y.sections,[-1,1]);
""")

    def test_the_popup_puts_the_terminal_cursor_after_the_text_before_the_place(self):
        out = self.node(r"""
  const x=setup({},[]);x.keys('두 번째 줄을 고칩니다'+HOME+RIGHT.repeat(4));
  const all=x.rows(60,10),popup=view.popup({cols:100,rows:30,kind:'question',title:all[0],body:all.slice(1),sections:['QUESTIONS'],current:0,cellWidth:measure});
  const at=all.slice(1).findIndex(r=>r.some(p=>p.caret)),row=all.slice(1)[at],front=row.slice(0,row.findIndex(p=>p.caret)+1).map(p=>p.text).join('');
  assert.equal(front,'› 두 번째');
  const line=popup.rows.map(r=>r.map(p=>p.text).join('')).find(s=>s.includes('두 번째 줄을 고칩니다'));assert(line);
  console.log(line.trim());
""")
        self.assertIn("› 두 번째 줄을 고칩니다", out)


if __name__ == "__main__":
    unittest.main()
