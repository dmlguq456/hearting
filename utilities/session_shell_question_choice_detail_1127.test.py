#!/usr/bin/env python3
"""11-27 (design 가, the person's choice): each choice's words stand under its name on the question board.

The person: "답변 선지가 클로드 네이티브 질문창이 좀 더 상세하네? 하팅 질문 판도 개선 필요", then, shown two sketches,
"가" (every choice's words under its name). A native question's option descriptions travel with their choices into
the Hearting question record (`choice_descriptions`, optional) instead of being listed in its text; `questions ask
--choice-description N=WORDS` gives them too; a frame question's `means` are drawn in the same place. The panel's
question popup, Fleet's unfolded question and the conversation card draw them alike; an answer never carries them.

These run the real native redirect (all three harnesses), the real record and read, the real backend presentation,
the real editor and popup frame, Fleet's real frame and the real card with synthetic values, and compare them cell
for cell with the sketch's text copies (`QUESTION-BOARD-CHOICE-DETAIL-1127.html`, `renders-text.json`). Where the
product's rule and the sketch differ the expected text is the product's, with the reason beside it.
"""
from __future__ import annotations
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import contextlib
import copy
import io
import json
from pathlib import Path
import re
import shutil
import subprocess
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SHELL = ROOT / "utilities/session-shell"
sys.path[:0] = [str(ROOT / "tools"), str(ROOT / "utilities"), str(ROOT)]

import human_wait_state as HWS  # noqa: E402
import native_question_redirect as redirect  # noqa: E402
import questions  # noqa: E402

SID = "choice-detail-session"
QUESTION = "견본 도감의 첫 장을 어떻게 구성할까요?"
OPTIONS = (("그림과 이름 (권장)", "그림 옆에 이름을 붙입니다."),
           ("관찰 기록", "잎의 색과 모양을 먼저 적습니다. 같은 종도 어디서 본 것인지 구분할 수 있습니다."),
           ("이름만", ""), ("수정", "원하는 배열을 답에 적습니다."))
# The eight-choice sketch: every choice's words are two lines (written so; each line is short).
EIGHT = (("그림과 이름 (권장)", "그림 옆에 이름을 붙입니다.\n표지는 한 장으로 둡니다."),
         ("관찰 기록", "관찰한 특징을 적습니다.\n잎과 줄기를 구분합니다."),
         ("이름만", "이름을 한 줄에 적습니다.\n그림은 다음 장에 둡니다."),
         ("색깔순", "비슷한 색을 모읍니다.\n옅은 색부터 놓습니다."),
         ("장소순", "장소별로 나눕니다.\n각 장소의 이름을 적습니다."),
         ("계절순", "계절별로 나눕니다.\n봄에 본 것부터 놓습니다."),
         ("날짜순", "날짜별로 나눕니다.\n먼저 본 것부터 놓습니다."),
         ("수정", "다른 배열을 적습니다.\n답은 여러 줄로 쓸 수 있습니다."))
DOWN, ENTER = "\x1b[B", "\r"
NODE = shutil.which("node") or "node"      # found before a fixture narrows PATH


def payload(harness, options=OPTIONS, text=QUESTION, **question):
    listed = [{"question": text, "header": "구성", "options": [
        {"label": label, **({"description": words} if words else {})} for label, words in options], **question}]
    if harness == "claude":
        return {"session_id": SID, "hook_event_name": "PreToolUse", "tool_name": "AskUserQuestion",
                "tool_input": {"questions": listed}}
    if harness == "codex":
        return {"session_id": SID, "hook_event_name": "PreToolUse", "tool_name": "request_user_input",
                "tool_input": {"questions": [{"id": "page", **listed[0]}]}}
    return {"tool": "question", "sessionID": SID, "args": {"questions": listed}}


# ---- the drawing, as the shell host lays it out (`host.cjs` `popupNow`) ----
POPUP = r"""
const dir=process.argv[1],a=JSON.parse(process.argv[2]),Module=require('node:module');
let create=require(dir+'/block-input.cjs');
if(a.source){const m=new Module(dir+'/block-input.cjs');m.filename=dir+'/block-input.cjs';m.paths=module.paths;m._compile(a.source,m.filename);create=m.exports;}
const view=require(dir+'/panel-view.cjs');
let sent=null;
const b=create({changed(){},action(){},answer:async x=>(sent=x,{state:'submitted'})});
b.update(a.items,'',{labels:a.labels});b.enter();
for(const key of a.keys){if(typeof key==='object'&&key.choose!==undefined)b.choose(key.choose);else b.input(Buffer.from(key,'utf8'));}
(async()=>{
const kind='question',width=view.popupWidth(a.cols,kind),inner=Math.max(0,width-4),room=view.popupRoom(a.rows,a.band);
const all=b.detail({budget:room+1,columns:inner}),title=all[0],body=all.slice(1);
const v=view.popup({cols:a.cols,rows:a.rows,band:a.band,kind,title,body,sections:a.sections,current:a.sections.indexOf('QUESTIONS'),model:{}});
const top=v.frame.top-v.top,left=v.frame.left-v.left;
let lines=v.rows.slice(top,top+v.frame.height).map(r=>{const t=r.map(p=>p.text).join('');return t.slice(left,left+[...t].length);});
const measure=s=>[...s].reduce((n,c)=>n+(c.codePointAt(0)>0x2e7f?2:1),0);
lines=lines.map(t=>{let out='',used=0;for(const c of t){if(used>=v.frame.width)break;out+=c;used+=measure(c);}return out;});
// The terminal's cursor stands where the host puts it while an answer is written: drawn here as `▏`.
const edit=b.typing?part=>part.caret===true:part=>String(part.text).startsWith('› ')&&String(part.text).length>2;
const at=body.slice(0,v.body).findIndex(row=>Array.isArray(row)&&row.some(edit));
let cursor=null;
if(at>=0&&b.typing){const parts=body[at],upto=parts.slice(0,parts.findIndex(edit)+1).map(p=>p.text).join('');
  cursor={row:at+1,col:2+Math.min(inner-1,measure(upto))};
  const chars=[...lines[cursor.row]];let used=0,i=0;for(;i<chars.length&&used<cursor.col;i++)used+=measure(chars[i]);
  if(chars[i]===' ')chars[i]='▏';lines[cursor.row]=chars.join('');}
const hits=v.hits.filter(h=>h.action.startsWith('answer-')).map(h=>({row:h.row-v.frame.top,action:h.action}));
const tinted=v.rows.slice(top,top+v.frame.height).map(r=>r.some(p=>p.background!==undefined&&p.chosenRow!==undefined));
// The words of the chosen choice read dim (the popup's floor gray); its name's row bright and bold.
const chosen=body.filter(r=>r.some(p=>p.tint)).flatMap(r=>r.filter(p=>String(p.text).trim()).map(p=>({text:p.text,dim:!!p.dim,bold:!!p.bold,role:p.role||''})));
for(const key of a.after||[])b.input(Buffer.from(key,'utf8'));
await new Promise(r=>setImmediate(r));
process.stdout.write(JSON.stringify({lines,hits,chosen,sent,summary:b.summary()}));
})().catch(e=>{console.error(e);process.exit(1)});
"""


def styled(display):
    from session_shell import styled_parts
    return {key: ([styled_parts(line, colors=256) for line in value] if key not in ("head", "brief_line")
                  else styled_parts(value, colors=256)) for key, value in display.items()}


def board_items(envelope, *, popup, panel=40):
    """The board's one item for this question, as the projection sends it to the shell."""
    from fleet.session_block import question_presentations
    items = [{"id": envelope.get("id") or envelope.get("request_id"), "revision": questions.request_revision(envelope),
              "answerable": True, "questions": questions.questions_of(envelope)}]
    with mock.patch.dict(os.environ, {"FLEET_NOW_LANG": "en"}):
        question_presentations(items, popup, brief_columns=panel - 4)
    for item in items:
        for question in item["questions"]:
            question["display"] = styled(question["display"])
    return items


def popup(envelope, *, cols, rows=46, band=1, keys=(), after=(), source=None, sections=("NOW", "QUESTIONS")):
    """The question popup's frame rows (and its presses, the chosen rows' parts, what Enter sent)."""
    import session_shell
    sys.path.insert(0, str(ROOT / "tools"))
    from session_shell import block_labels
    with mock.patch.dict(os.environ, {"FLEET_NOW_LANG": "en"}):
        import importlib
        width = cols - 4 if cols < 80 else min(132, cols - 4)
        labels = block_labels(width)
        items = board_items(envelope, popup=width)
    del session_shell, importlib
    args = {"items": items, "labels": labels, "cols": cols, "rows": rows, "band": band, "keys": list(keys),
            "after": list(after), "sections": list(sections), **({"source": source} if source else {})}
    done = subprocess.run([NODE, "-e", POPUP, str(SHELL), json.dumps(args, ensure_ascii=False)],
                          capture_output=True, text=True, timeout=60)
    if done.returncode:
        raise AssertionError(done.stdout + done.stderr)
    return json.loads(done.stdout)


def fleet(envelope, *, cols, height=30, mark=None):
    """Fleet's frame rows down to the unfolded question's `submit`, and its presses' rows."""
    from tools.fleet.explorer.frame import draw
    from tools.fleet.explorer.state import NavState
    from tools.fleet.explorer.view_model import ExplorerViewModel
    ref = {"id": envelope["request_id"], "harness": "claude", "session_id": SID,
           "request_id": envelope["request_id"], "question_id": "message-response"}
    row = {"id": ref["id"], "tag": ref["id"], "ref": ref, "kind": "question", "h": "claude",
           "session_tag": "f0", "source": "S4", "wait": "1m", "question": QUESTION}
    vm = ExplorerViewModel(questions=(row,), system={"answer_waits": {"state": "complete", "total": 1, "older": 0}})
    overlay = {"ref": ref, "detail": envelope, "draft": None, "state": "open", "editing": False,
               "pending": False, **({"choice_marks": {"message-response": mark}} if mark else {})}
    state = NavState(view="QUESTIONS", title="QUESTIONS", folds=(("qsel:QUESTIONS", ref["id"]),),
                     question_overlay=overlay)
    grid = draw(vm, state, cols, height, beat=1)
    rows = grid.text_rows()
    end = next(i for i, line in enumerate(rows) if line.strip().endswith("│     submit") or "│     submit" in line)
    return rows[:end + 1], grid, state, vm


def card(options, understanding="견본 도감의 첫 장을 정합니다.", text=QUESTION):
    import frame_interview as FI
    interview = {"understanding": understanding, "questions": [{"id": "q-page", "question": text, "options": [
        {"label": label, "means": words} for label, words in options]}]}
    with mock.patch.dict(os.environ, {"FLEET_NOW_LANG": "en"}):
        return FI.pending_question_block(interview)


# ---- what the product draws (the sketch's copies, with the product's rule where they differ) ----
A_POPUP_60 = """\
╭─ › ◑ 1 QUESTIONS · 1/1 · answer needed ────────────── ✕ ─╮
│ 견본 도감의 첫 장을 어떻게 구성할까요?                   │
│                                                          │
│ ──────────────────────────────────────────────────────── │
│   1  그림과 이름 (recommended)                           │
│      그림 옆에 이름을 붙입니다.                          │
│ ▸ 2  관찰 기록                                           │
│      잎의 색과 모양을 먼저 적습니다. 같은 종도 어디서 본 │
│       것인지 구분할 수 있습니다.                         │
│   3  이름만                                              │
│   4  수정                                                │
│      원하는 배열을 답에 적습니다.                        │
│ ›                                                        │
│ ↑↓ choose · type to write · Enter send · Esc/Ctrl+] back │
╰─ NOW · QUESTIONS ────────────────────────────────────────╯"""
A_POPUP_104 = """\
╭─ › ◑ 1 QUESTIONS · 1/1 · answer needed ────────────────────────────────────────────────────────── ✕ ─╮
│ 견본 도감의 첫 장을 어떻게 구성할까요?                                                               │
│                                                                                                      │
│ ──────────────────────────────────────────────────────────────────────────────────────────────────── │
│   1  그림과 이름 (recommended)                                                                       │
│      그림 옆에 이름을 붙입니다.                                                                      │
│ ▸ 2  관찰 기록                                                                                       │
│      잎의 색과 모양을 먼저 적습니다. 같은 종도 어디서 본 것인지 구분할 수 있습니다.                  │
│   3  이름만                                                                                          │
│   4  수정                                                                                            │
│      원하는 배열을 답에 적습니다.                                                                    │
│ ›                                                                                                    │
│ ↑↓ choose · type to write · Enter send · Tab next · Esc/Ctrl+] back                                  │
╰─ NOW · QUESTIONS ────────────────────────────────────────────────────────────────────────────────────╯"""
# Main's popup for a record without the words (an older record, or one whose words were written into its text by
# hand): the sketch's `current-popup`, unchanged.
CURRENT_POPUP = """\
╭─ › ◑ 1 QUESTIONS · 1/1 · answer needed ────────────── ✕ ─╮
│ 견본 도감의 첫 장을 어떻게 구성할까요?                   │
│ - 그림과 이름: 그림 옆에 이름을 붙입니다.                │
│ - 관찰 기록: 잎의 색과 모양을 먼저 적습니다. 같은 종도   │
│ 어디서 본 것인지 구분할 수 있습니다.                     │
│ - 수정: 원하는 배열을 답에 적습니다.                     │
│                                                          │
│ ──────────────────────────────────────────────────────── │
│ ▸ 1  그림과 이름 (recommended)                           │
│   2  관찰 기록                                           │
│   3  이름만                                              │
│   4  수정                                                │
│ ›                                                        │
│ ↑↓ choose · type to write · Enter send · Esc/Ctrl+] back │
╰─ NOW · QUESTIONS ────────────────────────────────────────╯"""
A_EIGHT_46 = """\
╭─ › ◑ 1 QUESTIONS · 1/1 · answer needed ────────────── ✕ ─╮
│ 견본 도감의 첫 장을 어떻게 구성할까요?                   │
│                                                          │
│ ──────────────────────────────────────────────────────── │
│ ▸ 1  그림과 이름 (recommended)                           │
│      그림 옆에 이름을 붙입니다.                          │
│      표지는 한 장으로 둡니다.                            │
│   2  관찰 기록                                           │
│      관찰한 특징을 적습니다.                             │
│      잎과 줄기를 구분합니다.                             │
│   3  이름만                                              │
│      이름을 한 줄에 적습니다.                            │
│      그림은 다음 장에 둡니다.                            │
│   4  색깔순                                              │
│      비슷한 색을 모읍니다.                               │
│      옅은 색부터 놓습니다.                               │
│   5  장소순                                              │
│      장소별로 나눕니다.                                  │
│      각 장소의 이름을 적습니다.                          │
│   6  계절순                                              │
│      계절별로 나눕니다.                                  │
│      봄에 본 것부터 놓습니다.                            │
│   7  날짜순                                              │
│      날짜별로 나눕니다.                                  │
│      먼저 본 것부터 놓습니다.                            │
│   8  수정                                                │
│      다른 배열을 적습니다.                               │
│      답은 여러 줄로 쓸 수 있습니다.                      │
│ ›                                                        │
│ ↑↓ choose · type to write · Enter send · Esc/Ctrl+] back │
╰─ NOW · QUESTIONS ────────────────────────────────────────╯"""
_EIGHT_ROWS = A_EIGHT_46.splitlines()
_MORE = "│ +1 · more choices · ↑↓" + " " * 34 + " │"
A_EIGHT_30 = "\n".join([*_EIGHT_ROWS[:2], *_EIGHT_ROWS[4:25], _MORE, _EIGHT_ROWS[28], _EIGHT_ROWS[30]])
A_EIGHT_30_END = "\n".join([*_EIGHT_ROWS[:2], *_EIGHT_ROWS[7:25], _EIGHT_ROWS[25].replace("│   8", "│ ▸ 8"),
                            *_EIGHT_ROWS[26:28], _MORE, _EIGHT_ROWS[28], _EIGHT_ROWS[30]])
# The sketch's `edit-popup` drew choices 1 and 2 without their words (sketch 나's form); under 가 every choice keeps
# its words while an answer is written, so they stand here. The rest is the sketch's.
EDIT_POPUP = """\
╭─ › ◑ 1 QUESTIONS · 1/1 · answer needed ────────────── ✕ ─╮
│ 견본 도감의 첫 장을 어떻게 구성할까요?                   │
│                                                          │
│ ──────────────────────────────────────────────────────── │
│   1  그림과 이름 (recommended)                           │
│      그림 옆에 이름을 붙입니다.                          │
│   2  관찰 기록                                           │
│      잎의 색과 모양을 먼저 적습니다. 같은 종도 어디서 본 │
│       것인지 구분할 수 있습니다.                         │
│   3  이름만                                              │
│ ▸ 4  수정                                                │
│      원하는 배열을 답에 적습니다.                        │
│ › 앞에는 이름만 두고                                     │
│   그림은 뒷장에 모아 주세요.▏                            │
│ Enter send · Ctrl+J newline · Esc/Ctrl+] back            │
╰─ NOW · QUESTIONS ────────────────────────────────────────╯"""
FRAME_MEANS = """\
╭─ › ◑ 1 QUESTIONS · 1/1 · answer needed ────────────── ✕ ─╮
│ 다음 검토에서 어디까지 확인할까요?                       │
│                                                          │
│ ──────────────────────────────────────────────────────── │
│ ▸ 1  첫 장만 (recommended)                               │
│      첫 장의 그림과 이름을 대조합니다.                   │
│   2  도감 전체                                           │
│      모든 장에서 같은 표기법을 확인합니다.               │
│   3  수정                                                │
│      다르게 볼 범위를 적습니다.                          │
│ ›                                                        │
│ ↑↓ choose · type to write · Enter send · Esc/Ctrl+] back │
╰─ NOW · QUESTIONS ────────────────────────────────────────╯"""
# The conversation card is plain text the agent leaves in its reply; the conversation's own window wraps a long line
# (the sketch drew it folded at 64 cells, two cells in), so the card holds each choice's words on one line.
A_CARD = """\
견본 도감의 첫 장을 정합니다.

견본 도감의 첫 장을 어떻게 구성할까요?
- 그림과 이름 (권장)
  그림 옆에 이름을 붙입니다.
- 관찰 기록
  잎의 색과 모양을 먼저 적습니다. 같은 종도 어디서 본 것인지 구분할 수 있습니다.
- 이름만
- 수정
  원하는 배열을 답에 적습니다."""
# Fleet's QUESTIONS view, the right side from its top line (the header row's release words and the side list are
# Fleet's own and not part of this change; the sketch's are those of its release).
A_FLEET = {
    80: """\
┌─ { QUESTIONS } ───────────────────────────────
│ ◑ to answer 1 ───────────────────────────────
│ ◑ [f0]  claude │ 견본 도감의 첫 장을 어…  1m
│     견본 도감의 첫 장을 어떻게 구성할까요?
│
│     ─────────────────────────────────────────
│     ○ 1  그림과 이름 (recommended)
│          그림 옆에 이름을 붙입니다.
│     ▸ 2  관찰 기록
│          잎의 색과 모양을 먼저 적습니다. 같은
│           종도 어디서 본 것인지 구분할 수 있
│          습니다.
│     ○ 3  이름만
│     ○ 4  수정
│          원하는 배열을 답에 적습니다.
│     > …
│     submit                                    """,
    100: """\
┌─ { QUESTIONS } ───────────────────────────────────────────────────
│ ◑ to answer 1 ───────────────────────────────────────────────────
│ ◑ [f0]  claude │ 견본 도감의 첫 장을 어떻게 구성할까요?       1m
│     견본 도감의 첫 장을 어떻게 구성할까요?
│
│     ─────────────────────────────────────────────────────────────
│     ○ 1  그림과 이름 (recommended)
│          그림 옆에 이름을 붙입니다.
│     ▸ 2  관찰 기록
│          잎의 색과 모양을 먼저 적습니다. 같은 종도 어디서 본 것인
│          지 구분할 수 있습니다.
│     ○ 3  이름만
│     ○ 4  수정
│          원하는 배열을 답에 적습니다.
│     > …
│     submit                                                        """,
    132: """\
┌─ { QUESTIONS } ───────────────────────────────────────────────────────────────────────────────────
│ ◑ to answer 1 ───────────────────────────────────────────────────────────────────────────────────
│ ◑ [f0]  claude │ 견본 도감의 첫 장을 어떻게 구성할까요?                                       1m
│     견본 도감의 첫 장을 어떻게 구성할까요?
│
│     ─────────────────────────────────────────────────────────────────────────────────────────────
│     ○ 1  그림과 이름 (recommended)
│          그림 옆에 이름을 붙입니다.
│     ▸ 2  관찰 기록
│          잎의 색과 모양을 먼저 적습니다. 같은 종도 어디서 본 것인지 구분할 수 있습니다.
│     ○ 3  이름만
│     ○ 4  수정
│          원하는 배열을 답에 적습니다.
│     > …
│     submit                                                                                        """,
}


def right_side(rows):
    """Fleet's rows from the QUESTIONS frame's left line on (the side list is left out), each without the
    window's blank cells after it."""
    at = rows[3].index("┌")
    return "\n".join(row[at:].rstrip() for row in rows[3:])


class Base(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        from fixtures.message_questions import MessageQuestionFixture
        import session_tidy as tidy
        from fleet import process_identity
        self.fixture = MessageQuestionFixture()
        self.f = self.fixture.__enter__()
        self.addCleanup(self.fixture.__exit__, None, None, None)
        for patch in (mock.patch.object(tidy, "resolve_seat",
                                        side_effect=lambda harness, *a, **k: self.f.seat(harness, SID)),
                      mock.patch.object(process_identity, "current_launch_context", return_value=None)):
            patch.start()
            self.addCleanup(patch.stop)

    def native(self, harness="claude", **kwargs):
        """A native question call redirected: the record it became, as the board reads it."""
        reason = redirect.redirect(harness, payload(harness, **kwargs))
        self.assertIsNotNone(reason, "the native call was not registered")
        item_id = re.search(r"(S4:[0-9a-f]{64})", reason).group(1)
        return self.envelope(harness, item_id)

    def envelope(self, harness, item_id):
        envelope = HWS.read_selected_request({"id": item_id, "harness": harness, "session_id": SID})
        return {**envelope, "id": item_id}

    def asked(self, text=QUESTION, choices=(), recommend=None, described=(), harness="claude"):
        """`hearting run questions ask` with these arguments: its record, as the board reads it."""
        import dispatch_parent_completion as parent
        argv = ["ask", text, *[arg for choice in choices for arg in ("--choice", choice)],
                *(["--recommend", str(recommend)] if recommend else []),
                *[arg for given in described for arg in ("--choice-description", given)], "--json"]
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(parent, "interactive_parent_identity", return_value=(harness, SID)), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = questions.main(argv)
        if code:
            return None, err.getvalue()
        return self.envelope(harness, json.loads(out.getvalue())["id"]), err.getvalue()

    def fresh(self):
        """A new private state root (an answer's delivery leaves its own records behind)."""
        from fixtures.message_questions import MessageQuestionFixture
        self.fixture.__exit__(None, None, None)
        self.fixture = MessageQuestionFixture()
        self.f = self.fixture.__enter__()
        self.addCleanup(self.fixture.__exit__, None, None, None)

    def forget(self):
        for signal in HWS.read_signals():
            HWS._remove_signal_source(signal, HWS._signal_path(signal))

    def text(self, drawn):
        return "\n".join(drawn["lines"])


class RecordAndRedirect(Base):
    def test_one_native_question_keeps_its_words_by_its_choices_on_every_harness(self):
        for harness in ("claude", "codex", "opencode"):
            with self.subTest(harness=harness):
                self.forget()
                envelope = self.native(harness)
                record = envelope["detail"]["questions"][0]
                self.assertEqual(record["question"], QUESTION)                   # the text is the question alone
                self.assertEqual(record["choices"], ["그림과 이름", "관찰 기록", "이름만", "수정"])
                self.assertEqual(record["recommended"], 0)
                self.assertEqual(record["choice_descriptions"],                 # the empty one left out
                                 {"그림과 이름": OPTIONS[0][1], "관찰 기록": OPTIONS[1][1], "수정": OPTIONS[3][1]})
                (question,) = questions.questions_of(envelope)
                self.assertEqual(question["choices"], record["choices"])
                self.assertEqual(question["descriptions"], [OPTIONS[0][1], OPTIONS[1][1], "", OPTIONS[3][1]])

    def test_the_three_harnesses_draw_one_popup(self):
        drawn = set()
        for harness in ("claude", "codex", "opencode"):
            self.forget()
            drawn.add(self.text(popup(self.native(harness), cols=64, keys=[DOWN])))
        self.assertEqual(drawn, {A_POPUP_60})

    def test_several_questions_multi_select_or_a_preview_keep_the_listing_in_the_text(self):
        many = {"questions": [{"question": QUESTION, "options": [{"label": l, "description": d} for l, d in OPTIONS]},
                              {"question": "두 번째?", "options": [{"label": "예"}, {"label": "아니요"}]}]}
        cases = (("several", many),
                 ("multi", {"questions": [{"question": QUESTION, "multiSelect": True,
                                           "options": [{"label": l, "description": d} for l, d in OPTIONS]}]}),
                 ("preview", {"questions": [{"question": QUESTION, "options": [
                     {"label": l, "description": d, **({"preview": "견본 그림"} if not n else {})}
                     for n, (l, d) in enumerate(OPTIONS)]}]}))
        for name, listed in cases:
            with self.subTest(name):
                self.forget()
                reason = redirect.redirect("claude", {"session_id": SID, "tool_name": "AskUserQuestion",
                                                      "tool_input": listed})
                envelope = self.envelope("claude", re.search(r"(S4:[0-9a-f]{64})", reason).group(1))
                record = envelope["detail"]["questions"][0]
                self.assertEqual(record["choices"], [])
                self.assertNotIn("choice_descriptions", record)
                self.assertIn(" — " + OPTIONS[1][1], record["question"])        # the text is their one source

    def test_too_large_registers_again_without_the_words(self):
        long = "긴 설명 " * 500
        envelope = self.native(options=(("그림과 이름", long), ("관찰 기록", "짧은 설명")))
        record = envelope["detail"]["questions"][0]
        self.assertEqual((record["question"], record["choices"]), (QUESTION, ["그림과 이름", "관찰 기록"]))
        self.assertNotIn("choice_descriptions", record)
        self.assertNotIn("descriptions", questions.questions_of(envelope)[0])

    def test_a_question_without_words_is_the_record_main_writes(self):
        envelope = self.native(options=tuple((label, "") for label, _words in OPTIONS))
        record = envelope["detail"]["questions"][0]
        self.assertEqual(set(record), {"id", "question", "choices", "recommended", "is_secret", "is_other", "required"})
        self.assertNotIn("descriptions", questions.questions_of(envelope)[0])

    def test_questions_ask_takes_the_words_by_choice_number(self):
        envelope, err = self.asked(choices=[label for label, _ in OPTIONS], recommend=1,
                                   described=["2=" + OPTIONS[1][1], "1=" + OPTIONS[0][1], "4=" + OPTIONS[3][1]])
        self.assertEqual(err, "")
        record = envelope["detail"]["questions"][0]
        self.assertEqual(list(record["choice_descriptions"]), ["그림과 이름 (권장)", "관찰 기록", "수정"])
        self.assertEqual(questions.questions_of(envelope)[0]["descriptions"],
                         [OPTIONS[0][1], OPTIONS[1][1], "", OPTIONS[3][1]])
        self.forget()
        plain, _ = self.asked(choices=["가", "나"])
        self.assertEqual(set(plain["detail"]["questions"][0]),
                         {"id", "question", "choices", "recommended", "is_secret", "is_other", "required"})
        for wrong in ("3=셋째", "0=영", "x=틀림", "2", "1=하나"):
            with self.subTest(wrong=wrong):
                self.forget()
                envelope, err = self.asked(choices=["가", "나"], described=[wrong, "1=다시"] if wrong == "1=하나" else [wrong])
                self.assertIsNone(envelope)
                self.assertIn("--choice-description", err)
        parser_help = io.StringIO()
        with contextlib.redirect_stdout(parser_help), self.assertRaises(SystemExit):
            questions.main(["ask", "--help"])
        self.assertIn("words shown under choice N (1-based; repeat; optional)", " ".join(parser_help.getvalue().split()))

    def test_the_list_shows_the_words_under_their_choice(self):
        envelope = self.native()
        row = {"id": envelope["id"], "kind": "message question", "age": "1m", "project": None, "state": "current",
               "owner": {"harness": "claude", "session_id": SID}, "owner_state": "current",
               "questions": questions.questions_of(envelope), "answerable": False}
        text = questions.render({"items": [], "complete": True, "observed_at": 0}, [row], [])
        self.assertIn("      2) 관찰 기록\n         " + OPTIONS[1][1] + "\n      3) 이름만\n      4) 수정\n         ", text)


class Answers(Base):
    """What an answer sends is what main sends: the choice's value, its number's value, the words written."""

    def sent(self, envelope, response, action):
        signal = next(x for x in HWS.read_signals() if x["id"] == envelope["id"])
        result = HWS.submit_selected_response(envelope, response, action_id=action)
        self.assertEqual(result["state"], "submitted", result)
        calls, _, pending = self.f.courier(signal)
        # The record's id and the hashes drawn from it differ by design; every other byte is compared.
        return re.sub(r"[0-9a-f]{32,64}", "<hash>", pending["text"].replace(envelope["id"], "<id>"))

    def test_number_name_and_words_send_the_same_as_a_record_without_words(self):
        wires = {}
        for described in (True, False):
            for name, response, typed in (("choice", {"answers": {"message-response": ["관찰 기록"]}}, "2"),
                                          ("text", {"answers": {"message-response": ["수정", "앞에는 이름만"]}}, "관찰 기록."),
                                          ("words", {"answers": {"message-response": ["원하는 대로"]}}, "수정")):
                self.fresh()
                envelope = self.native(options=OPTIONS if described else tuple((l, "") for l, _ in OPTIONS))
                signal = next(x for x in HWS.read_signals() if x["id"] == envelope["id"])
                wires[(described, name, "typed")] = HWS.message_prompt_answer(signal, typed)
                wires[(described, name, "words typed")] = HWS.message_prompt_answer(signal, OPTIONS[1][1])
                wires[(described, name)] = self.sent(envelope, response, name + str(described))
        for key in [k for k in wires if k[0]]:
            with self.subTest(key=key):
                self.assertEqual(wires[key], wires[(False, *key[1:])])
        self.assertEqual([wires[(True, n, "typed")] for n in ("choice", "text", "words")], ["관찰 기록", "관찰 기록", "수정"])
        for wire in (wires[(True, n)] for n in ("choice", "text", "words")):
            self.assertNotIn("잎의 색", wire)
        self.assertIsNone(wires[(True, "choice", "words typed")])            # the words are no answer

    def test_the_popup_sends_the_choice_alone(self):
        for keys, value in (([DOWN, ENTER], ["관찰 기록"]), (["4", "다르게", ENTER], ["수정", "다르게"]),
                            (["2", ENTER], ["관찰 기록"])):
            with self.subTest(keys=keys):
                self.forget()
                drawn = popup(self.native(), cols=64, keys=keys)
                self.assertEqual(drawn["sent"]["answers"]["message-response"], value)


class Drawn(Base):
    def test_the_popup_at_60_and_104_cells(self):
        envelope = self.native()
        self.assertEqual(self.text(popup(envelope, cols=64, keys=[DOWN])), A_POPUP_60)
        self.assertEqual(self.text(popup(envelope, cols=108, keys=[DOWN])), A_POPUP_104)

    def test_the_words_belong_to_their_choice(self):
        drawn = popup(self.native(), cols=64, keys=[DOWN])
        lines = drawn["lines"]
        hits = {hit["row"]: hit["action"] for hit in drawn["hits"]}
        rows = {n: [i for i, line in enumerate(lines) if line.startswith(("│   " + n, "│ ▸ " + n))][0] for n in "1234"}
        self.assertEqual({row: hits.get(row) for row in range(rows["1"], rows["4"] + 2)},
                         {rows["1"]: "answer-choice:0", rows["1"] + 1: "answer-choice:0",
                          rows["2"]: "answer-choice:1", rows["2"] + 1: "answer-choice:1", rows["2"] + 2: "answer-choice:1",
                          rows["3"]: "answer-choice:2", rows["4"]: "answer-choice:3", rows["4"] + 1: "answer-choice:3"})
        # The chosen choice's rows: its name bright and bold, its words dim (the popup's floor gray), on one ground.
        self.assertEqual([(p["text"].strip(), p["dim"], p["bold"]) for p in drawn["chosen"]
                          if p["role"] == "choice-detail"],
                         [("잎의 색과 모양을 먼저 적습니다. 같은 종도 어디서 본", True, False), ("것인지 구분할 수 있습니다.", True, False)])
        self.assertTrue(any(p["text"] == "관찰 기록" and p["bold"] and not p["dim"] for p in drawn["chosen"]))

    def test_eight_choices_with_two_lines_each_at_46_and_30_rows(self):
        envelope = self.native(options=EIGHT)
        self.assertEqual(self.text(popup(envelope, cols=64, rows=46)), A_EIGHT_46)
        self.assertEqual(self.text(popup(envelope, cols=64, rows=30)), A_EIGHT_30)
        self.assertEqual(self.text(popup(envelope, cols=64, rows=30, keys=[DOWN] * 7)), A_EIGHT_30_END)
        # From the first choice down to the last the highlighted one is always among whole choices.
        for step in range(8):
            with self.subTest(step=step):
                lines = popup(envelope, cols=64, rows=30, keys=[DOWN] * step)["lines"]
                shown = [int(m.group(1)) for line in lines if (m := re.match(r"│ [▸ ] (\d)  ", line))]
                self.assertIn(step + 1, shown)
                self.assertEqual(len(shown), 7)
                self.assertEqual(shown, list(range(shown[0], shown[0] + 7)))
                for n in shown:          # each shown choice has both its lines
                    at = next(i for i, line in enumerate(lines) if re.match(r"│ [▸ ] %d  " % n, line))
                    self.assertEqual([lines[at + 1][7:-1].strip(), lines[at + 2][7:-1].strip()],
                                     EIGHT[n - 1][1].split("\n"))
                self.assertIn("│ +1 · more choices · ↑↓", "\n".join(lines))

    def test_choosing_edit_and_writing(self):
        drawn = popup(self.native(), cols=64, keys=["4", "앞에는 이름만 두고", "\n", "그림은 뒷장에 모아 주세요."])
        self.assertEqual(self.text(drawn), EDIT_POPUP)

    def test_frame_means_are_drawn_where_a_native_description_is(self):
        frame = {"kind": "question", "state": "open", "request_id": "S2:fixture", "native_ref": {"transport": "workflow-gate"},
                 "detail": {"gate": "frame-review", "response_mode": "frame-interview", "questions": [
                     {"id": "q-scope", "question": "다음 검토에서 어디까지 확인할까요?", "recommended": 0, "is_other": True,
                      "choices": [{"value": "첫 장만", "label": "첫 장만", "means": "첫 장의 그림과 이름을 대조합니다."},
                                  {"value": "도감 전체", "label": "도감 전체", "means": "모든 장에서 같은 표기법을 확인합니다."},
                                  {"value": "수정", "label": "수정", "means": "다르게 볼 범위를 적습니다.",
                                   "description": ""}]}]}}
        self.assertEqual(self.text(popup(frame, cols=64)), FRAME_MEANS)
        both = copy.deepcopy(frame)
        both["detail"]["questions"][0]["choices"][1]["description"] = "모든 장을 봅니다."
        lines = popup(both, cols=64)["lines"]
        self.assertIn("│      모든 장을 봅니다.", "\n".join(lines))            # the description first, once
        self.assertNotIn("같은 표기법", "\n".join(lines))

    def test_fleet_at_80_100_and_132_cells(self):
        envelope = self.native()
        for cols, expected in A_FLEET.items():
            with self.subTest(cols=cols):
                rows, *_ = fleet(envelope, cols=cols, mark=(1, True))
                self.assertEqual(right_side(rows), "\n".join(line.rstrip() for line in expected.splitlines()))

    def test_fleet_presses_on_a_choices_words_pick_that_choice(self):
        from tools.fleet.explorer.state import reduce
        envelope = self.native()
        rows, grid, state, _vm = fleet(envelope, cols=100, mark=(1, True))
        at = next(i for i, line in enumerate(rows) if "잎의 색과 모양" in line)
        for row in (at, at + 1):
            self.assertEqual([hit.action for hit in grid.hits if hit.y == row],
                             [("question.choice", "message-response", "관찰 기록")])
        later = next(i for i, line in enumerate(rows) if "원하는 배열을" in line)
        self.assertEqual([hit.action for hit in grid.hits if hit.y == later],
                         [("question.choice", "message-response", "수정")])
        state, _effects = reduce(state, {"type": "click", "action": ("question.choice", "message-response", "수정")})
        self.assertEqual(state.question_overlay["draft"]["answers"]["message-response"], ["수정"])

    def test_the_card_puts_each_choices_words_under_it(self):
        self.assertEqual(card(OPTIONS), A_CARD)

    def test_the_sessions_summary_is_the_question_and_the_names(self):
        (item,) = board_items(self.native(), popup=60, panel=66)
        brief = item["questions"][0]["display"]["brief"]
        self.assertEqual(["".join(p["text"] for p in row) for row in brief],
                         ["◑ 견본 도감의 첫 장을 어떻게 구성할까요?",
                          "  1 그림과 이름 (recommended) · 2 관찰 기록 · 3 이름만 · +1"])


class Edges(Base):
    def test_an_older_record_draws_as_on_main(self):
        # Main's redirect wrote the words into the text; the hand-written lines are words like any other.
        text = QUESTION + "".join(f"\n- {label.replace(' (권장)', '')}: {words}" for label, words in OPTIONS if words)
        envelope, _ = self.asked(text=text, choices=[label.replace(" (권장)", "") for label, _ in OPTIONS], recommend=1)
        self.assertEqual(self.text(popup(envelope, cols=64)), CURRENT_POPUP)
        rows, *_ = fleet(envelope, cols=100)
        self.assertIn("│     - 관찰 기록: 잎의 색과 모양을 먼저 적습니다. 같은 종도 어디서", "\n".join(rows))

    def test_one_choice_double_width_a_long_description_and_a_bare_question(self):
        long = "매우 긴 설명입니다. " * 14
        envelope = self.native(options=(("하나뿐 🙂 선지", long),), text="짧은 질문")
        lines = popup(envelope, cols=64)["lines"]
        at = next(i for i, line in enumerate(lines) if "하나뿐 🙂 선지" in line)
        words = [line[7:-1] for line in lines[at + 1:] if line.startswith("│      ")]
        self.assertGreater(len(words), 4)                                       # more than four rows, wrapped
        self.assertEqual("".join(w.rstrip() if i == len(words) - 1 else w for i, w in enumerate(words)).replace(" ", ""),
                         long.strip().replace(" ", ""))
        self.assertEqual(lines[1], "│ 짧은 질문" + " " * 48 + "│")
        rows, *_ = fleet(envelope, cols=80)
        self.assertGreater(sum("매우 긴" in line or "설명입니" in line or "니다." in line for line in rows), 4)

    def test_no_words_is_the_popup_main_draws(self):
        envelope = self.native(options=tuple((label, "") for label, _ in OPTIONS))
        lines = popup(envelope, cols=64)["lines"]
        self.assertEqual(lines[4:8], ["│ ▸ 1  그림과 이름 (recommended)                           │",
                                      "│   2  관찰 기록                                           │",
                                      "│   3  이름만                                              │",
                                      "│   4  수정                                                │"])


class Mutations(Base):
    """Each guard above fails where the change it holds is undone."""

    def test_the_words_written_into_the_text_again(self):
        built = redirect.build

        def old(rows, *, detail=True):
            value = built(rows, detail=detail)
            if value["descriptions"]:
                value = {**value, "text": value["text"] + "".join(f"\n- {k}: {v}" for k, v in value["descriptions"].items())}
            return value
        with mock.patch.object(redirect, "build", old):
            envelope = self.native()
        self.assertNotEqual(self.text(popup(envelope, cols=64, keys=[DOWN])), A_POPUP_60)

    def test_the_words_joined_to_the_answer(self):
        built = redirect.build

        def joined(rows, *, detail=True):
            value = built(rows, detail=detail)
            words = value["descriptions"] or {}
            return {**value, "choices": [c + (" — " + words[c] if c in words else "") for c in value["choices"]],
                    "descriptions": None}
        with mock.patch.object(redirect, "build", joined):
            envelope = self.native()
        drawn = popup(envelope, cols=64, keys=[DOWN, ENTER])
        self.assertNotEqual(drawn["sent"]["answers"]["message-response"], ["관찰 기록"])

    def test_presses_on_the_name_rows_alone(self):
        source = (SHELL / "block-input.cjs").read_text()
        cut = source.replace("gap={...span('  ',''),...pick}", "gap=span('  ','')")
        self.assertNotEqual(cut, source)
        drawn = popup(self.native(), cols=64, keys=[DOWN], source=cut)
        rows = [hit["row"] for hit in drawn["hits"] if hit["action"] == "answer-choice:1"]
        self.assertEqual(len(rows), 1)                                          # the guard's three rows fail

    def test_fleet_presses_on_the_name_rows_alone(self):
        import inspect
        import textwrap
        from tools.fleet.explorer import tables
        source = textwrap.dedent(inspect.getsource(tables._question_body))
        cut = source.replace('''                if selectable:
                    # Its words' rows answer as its name's row does.
                    grid.add_hit(y, x, right, ("question.choice", qid, value))
''', "")
        self.assertNotEqual(cut, source)
        space = dict(vars(tables))
        exec(compile(cut, tables.__file__, "exec"), space)
        with mock.patch.object(tables, "_question_body", space["_question_body"]):
            rows, grid, *_ = fleet(self.native(), cols=100, mark=(1, True))
        at = next(i for i, line in enumerate(rows) if "잎의 색과 모양" in line)
        self.assertEqual([hit.action for hit in grid.hits if hit.y == at], [])   # the guard's rows fail

    def test_hand_written_lines_dropped_from_an_older_record(self):
        import fleet.session_block as SB
        presented = SB.question_presentations

        def dropping(items, *a, **k):
            for item in items:
                for question in item.get("questions") or ():
                    question["question"] = "\n".join(line for line in question["question"].splitlines()
                                                     if not line.startswith("- "))
            return presented(items, *a, **k)
        text = QUESTION + "".join(f"\n- {label.replace(' (권장)', '')}: {words}" for label, words in OPTIONS if words)
        envelope, _ = self.asked(text=text, choices=[label.replace(" (권장)", "") for label, _ in OPTIONS], recommend=1)
        with mock.patch.object(SB, "question_presentations", dropping):
            self.assertNotEqual(self.text(popup(envelope, cols=64)), CURRENT_POPUP)

    def test_the_chosen_words_bold(self):
        source = (SHELL / "block-input.cjs").read_text()
        cut = source.replace("part.role==='choice-detail'?dim(part):", "false?dim(part):")
        self.assertNotEqual(cut, source)
        drawn = popup(self.native(), cols=64, keys=[DOWN], source=cut)
        self.assertTrue(any(p["role"] == "choice-detail" and p["bold"] for p in drawn["chosen"]))

    def test_sliding_by_rows_again(self):
        source = (SHELL / "block-input.cjs").read_text()
        cut = source.replace("const groups=choiceGroups(options)", "const groups=[]")
        self.assertNotEqual(cut, source)
        envelope = self.native(options=EIGHT)
        self.assertNotEqual(self.text(popup(envelope, cols=64, rows=30, source=cut)), A_EIGHT_30)


if __name__ == "__main__":
    unittest.main()
