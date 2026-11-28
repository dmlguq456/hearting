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
GIT = shutil.which("git") or "git"


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
for(const key of a.keys){if(typeof key==='object'&&key.choose!==undefined)b.choose(key.choose);else if(typeof key==='object'&&key.write)b.write();else b.input(Buffer.from(key,'utf8'));}
(async()=>{
const kind='question',width=view.popupWidth(a.cols,kind),inner=Math.max(0,width-4),room=view.popupRoom(a.rows,a.band);
const all=b.detail({budget:room+1,columns:inner}),title=all[0],body=all.slice(1);
const v=view.popup({cols:a.cols,rows:a.rows,band:a.band,kind,title,body,sections:a.sections,current:a.sections.indexOf('QUESTIONS'),model:{}});
const top=v.frame.top-v.top,left=v.frame.left-v.left;
let lines=v.rows.slice(top,top+v.frame.height).map(r=>{const t=r.map(p=>p.text).join('');return t.slice(left,left+[...t].length);});
const measure=s=>[...s].reduce((n,c)=>n+(c.codePointAt(0)>0x2e7f?2:1),0);
lines=lines.map(t=>{let out='',used=0;for(const c of t){if(used>=v.frame.width)break;out+=c;used+=measure(c);}return out;});
// The terminal's cursor stands where the host puts it (the editor's own judgment, `host.cjs` `popupNow`; an editor
// from before it had one, the host's rule of then); while an answer is written it is drawn here as `▏`.
const edit=b.editPart?part=>b.editPart(part):b.typing?part=>part.caret===true:part=>String(part.text).startsWith('› ')&&String(part.text).length>2;
const at=body.slice(0,v.body).findIndex(row=>Array.isArray(row)&&row.some(edit));
let cursor=null;
if(at>=0){const parts=body[at],upto=parts.slice(0,parts.findIndex(edit)+1).map(p=>p.text).join('');
  cursor={row:at+1,col:2+Math.min(inner-1,measure(upto))};}
if(cursor&&b.typing){
  const chars=[...lines[cursor.row]];let used=0,i=0;for(;i<chars.length&&used<cursor.col;i++)used+=measure(chars[i]);
  if(chars[i]===' ')chars[i]='▏';lines[cursor.row]=chars.join('');}
const hits=v.hits.filter(h=>h.action.startsWith('answer-')).map(h=>({row:h.row-v.frame.top,action:h.action}));
const tinted=v.rows.slice(top,top+v.frame.height).map(r=>r.some(p=>p.background!==undefined&&p.chosenRow!==undefined));
// The words of the chosen choice read dim (the popup's floor gray); its name's row bright and bold.
const chosen=body.filter(r=>r.some(p=>p.tint)).flatMap(r=>r.filter(p=>String(p.text).trim()).map(p=>({text:p.text,dim:!!p.dim,bold:!!p.bold,role:p.role||''})));
for(const key of a.after||[])b.input(Buffer.from(key,'utf8'));
await new Promise(r=>setImmediate(r));
process.stdout.write(JSON.stringify({lines,hits,chosen,sent,cursor,summary:b.summary()}));
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


def fleet(envelope, *, cols, height=30, mark=None, whole=False, extra=None):
    """Fleet's frame rows down to the unfolded question's `submit`, and its presses' rows."""
    from tools.fleet.explorer.frame import draw
    from tools.fleet.explorer.state import NavState
    from tools.fleet.explorer.view_model import ExplorerViewModel
    ref = {"id": envelope["request_id"], "harness": "claude", "session_id": SID,
           "request_id": envelope["request_id"], "question_id": "message-response"}
    row = {"id": ref["id"], "tag": ref["id"], "ref": ref, "kind": "question", "h": "claude",
           "session_tag": "f0", "source": "S4", "wait": "1m", "question": QUESTION, **(extra or {})}
    vm = ExplorerViewModel(questions=(row,), system={"answer_waits": {"state": "complete", "total": 1, "older": 0}})
    overlay = {"ref": ref, "detail": envelope, "draft": None, "state": "open", "editing": False,
               "pending": False, **({"choice_marks": {"message-response": mark}} if mark else {})}
    state = NavState(view="QUESTIONS", title="QUESTIONS", folds=(("qsel:QUESTIONS", ref["id"]),),
                     question_overlay=overlay)
    grid = draw(vm, state, cols, height, beat=1)
    rows = grid.text_rows()
    if whole:
        return rows, grid, state, vm
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
╭─ ▶ ◑ 1 QUESTIONS · 1/1 · answer needed ────────────── ✕ ─╮
│ 견본 도감의 첫 장을 어떻게 구성할까요?                   │
│                                                          │
│ ──────────────────────────────────────────────────────── │
│   1  그림과 이름 (recommended)                           │
│      그림 옆에 이름을 붙입니다.                          │
│ ▶ 2  관찰 기록                                           │
│      잎의 색과 모양을 먼저 적습니다. 같은 종도 어디서 본 │
│      것인지 구분할 수 있습니다.                          │
│   3  이름만                                              │
│   4  수정                                                │
│      원하는 배열을 답에 적습니다.                        │
│ ›                                                        │
│ ↑↓ choose · type to write · Enter send · Esc/Ctrl+] back │
╰─ NOW · QUESTIONS ────────────────────────────────────────╯"""
A_POPUP_104 = """\
╭─ ▶ ◑ 1 QUESTIONS · 1/1 · answer needed ────────────────────────────────────────────────────────── ✕ ─╮
│ 견본 도감의 첫 장을 어떻게 구성할까요?                                                               │
│                                                                                                      │
│ ──────────────────────────────────────────────────────────────────────────────────────────────────── │
│   1  그림과 이름 (recommended)                                                                       │
│      그림 옆에 이름을 붙입니다.                                                                      │
│ ▶ 2  관찰 기록                                                                                       │
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
╭─ ▶ ◑ 1 QUESTIONS · 1/1 · answer needed ────────────── ✕ ─╮
│ 견본 도감의 첫 장을 어떻게 구성할까요?                   │
│ - 그림과 이름: 그림 옆에 이름을 붙입니다.                │
│ - 관찰 기록: 잎의 색과 모양을 먼저 적습니다. 같은 종도   │
│ 어디서 본 것인지 구분할 수 있습니다.                     │
│ - 수정: 원하는 배열을 답에 적습니다.                     │
│                                                          │
│ ──────────────────────────────────────────────────────── │
│ ▶ 1  그림과 이름 (recommended)                           │
│   2  관찰 기록                                           │
│   3  이름만                                              │
│   4  수정                                                │
│ ›                                                        │
│ ↑↓ choose · type to write · Enter send · Esc/Ctrl+] back │
╰─ NOW · QUESTIONS ────────────────────────────────────────╯"""
A_EIGHT_46 = """\
╭─ ▶ ◑ 1 QUESTIONS · 1/1 · answer needed ────────────── ✕ ─╮
│ 견본 도감의 첫 장을 어떻게 구성할까요?                   │
│                                                          │
│ ──────────────────────────────────────────────────────── │
│ ▶ 1  그림과 이름 (recommended)                           │
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
# 30 rows (review 2 of #1908): laid out as the same question without words (every name, the empty row, the line and
# the keys fit), the rows left take the words: the highlighted choice's first, then the others from the top. The
# sketch's `a-eight-30` (whole choices sliding, `+1`) was a different rule; the person's rule now is this one.
A_EIGHT_30 = """\
╭─ ▶ ◑ 1 QUESTIONS · 1/1 · answer needed ────────────── ✕ ─╮
│ 견본 도감의 첫 장을 어떻게 구성할까요?                   │
│                                                          │
│ ──────────────────────────────────────────────────────── │
│ ▶ 1  그림과 이름 (recommended)                           │
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
│   8  수정                                                │
│ ›                                                        │
│ ↑↓ choose · type to write · Enter send · Esc/Ctrl+] back │
╰─ NOW · QUESTIONS ────────────────────────────────────────╯"""
A_EIGHT_30_END = """\
╭─ ▶ ◑ 1 QUESTIONS · 1/1 · answer needed ────────────── ✕ ─╮
│ 견본 도감의 첫 장을 어떻게 구성할까요?                   │
│                                                          │
│ ──────────────────────────────────────────────────────── │
│   1  그림과 이름 (recommended)                           │
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
│   7  날짜순                                              │
│ ▶ 8  수정                                                │
│      다른 배열을 적습니다.                               │
│      답은 여러 줄로 쓸 수 있습니다.                      │
│ ›                                                        │
│ ↑↓ choose · type to write · Enter send · Esc/Ctrl+] back │
╰─ NOW · QUESTIONS ────────────────────────────────────────╯"""
# The sketch's `edit-popup` drew choices 1 and 2 without their words (sketch 나's form); under 가 every choice keeps
# its words while an answer is written, so they stand here. The rest is the sketch's.
EDIT_POPUP = """\
╭─ ▶ ◑ 1 QUESTIONS · 1/1 · answer needed ────────────── ✕ ─╮
│ 견본 도감의 첫 장을 어떻게 구성할까요?                   │
│                                                          │
│ ──────────────────────────────────────────────────────── │
│   1  그림과 이름 (recommended)                           │
│      그림 옆에 이름을 붙입니다.                          │
│   2  관찰 기록                                           │
│      잎의 색과 모양을 먼저 적습니다. 같은 종도 어디서 본 │
│      것인지 구분할 수 있습니다.                          │
│   3  이름만                                              │
│ ▶ 4  수정                                                │
│      원하는 배열을 답에 적습니다.                        │
│ › 앞에는 이름만 두고                                     │
│   그림은 뒷장에 모아 주세요.▏                            │
│ Enter send · Ctrl+J newline · Esc/Ctrl+] back            │
╰─ NOW · QUESTIONS ────────────────────────────────────────╯"""
FRAME_MEANS = """\
╭─ ▶ ◑ 1 QUESTIONS · 1/1 · answer needed ────────────── ✕ ─╮
│ 다음 검토에서 어디까지 확인할까요?                       │
│                                                          │
│ ──────────────────────────────────────────────────────── │
│ ▶ 1  첫 장만 (recommended)                               │
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
│     ▶ 2  관찰 기록
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
│     ▶ 2  관찰 기록
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
│     ▶ 2  관찰 기록
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
        """Withdraw every open question: a message question is closed (it is never removed)."""
        for signal in HWS.read_signals():
            if signal.get("mode") == "message":
                HWS.close_message(signal["harness"], signal["session_id"], signal["seat_key"], signal["id"])
            else:
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

    def test_a_multi_select_or_a_preview_keeps_the_listing_in_the_text_and_several_are_one_each(self):
        many = {"questions": [{"question": QUESTION, "options": [{"label": l, "description": d} for l, d in OPTIONS]},
                              {"question": "두 번째?", "options": [{"label": "예"}, {"label": "아니요"}]}]}
        # 11-28 (piece 3b, items): several questions are one question each, so each keeps its choices
        reason = redirect.redirect("claude", {"session_id": SID, "tool_name": "AskUserQuestion", "tool_input": many})
        first, second = (self.envelope("claude", found)["detail"]["questions"][0]
                         for found in re.findall(r"S4:[0-9a-f]{64}", reason))
        self.assertEqual((first["question"], first["choices"], first["recommended"]),
                         (QUESTION, ["그림과 이름", "관찰 기록", "이름만", "수정"], 0))
        self.assertEqual(first["choice_descriptions"][OPTIONS[1][0]], OPTIONS[1][1])
        self.assertEqual((second["question"], second["choices"]), ("두 번째?", ["예", "아니요"]))
        cases = (("multi", {"questions": [{"question": QUESTION, "multiSelect": True,
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
                # each ask is a later prompt: the same registration at one prompt reads back its closed record
                import session_tidy as tidy
                import time
                tidy.bump_prompt_seq(self.f.seat("claude", SID), "claude", SID, time.time())
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
        rows = {n: [i for i, line in enumerate(lines) if line.startswith(("│   " + n, "│ ▶ " + n))][0] for n in "1234"}
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
        # From the first choice down to the last: every choice name shows (all fit without words), and the
        # highlighted one's words come first.
        for step in range(8):
            with self.subTest(step=step):
                lines = popup(envelope, cols=64, rows=30, keys=[DOWN] * step)["lines"]
                self.assertEqual(shown_choices(lines), list(range(1, 9)))
                at = next(i for i, line in enumerate(lines) if line.startswith("│ ▶ %d  " % (step + 1)))
                self.assertEqual(lines[at + 1][7:-1].strip(), EIGHT[step][1].split("\n")[0])

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

    def test_the_sessions_summary_is_the_question_alone(self):
        # 11-28 (the person: "선지는 미리 보기를 날리고"): the panel's row of the question is its first line; the
        # choices' names and words are the popup's alone.
        (item,) = board_items(self.native(), popup=60, panel=66)
        display = item["questions"][0]["display"]
        self.assertEqual("".join(p["text"] for p in display["brief_line"]), "견본 도감의 첫 장을 어떻게 구성할까요?")
        self.assertNotIn("brief", display)


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
        self.assertEqual(lines[4:8], ["│ ▶ 1  그림과 이름 (recommended)                           │",
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
        cut = source.replace('''                grid.put(indent + 5, y, detail, "dim", limit=right)
                if selectable:
                    grid.add_hit(y, x, right, ("question.choice", qid, value))
''', '''                grid.put(indent + 5, y, detail, "dim", limit=right)
''')
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

# ---- review of #1908 (head aa20fe83e), its five must-fix findings ----
# The panel editor without choice words: #1905's head (the eight marks, `▶`), merged here ahead of main.
BASE = "c2e551f1e"


_MAIN_EDITOR = []


def main_editor():
    """Main's panel editor source, read once before a fixture narrows the environment git reads."""
    if not _MAIN_EDITOR:
        _MAIN_EDITOR.append(subprocess.check_output([GIT, "show", BASE + ":utilities/session-shell/block-input.cjs"],
                                                    cwd=ROOT, text=True, timeout=30))
    return _MAIN_EDITOR[0]


def shown_choices(lines):
    return [int(m.group(1)) for line in lines if (m := re.match(r"│ [▶ ] (\d)  ", line))]


def fleet_choice_rows(rows):
    """``{row: choice number}`` for each row Fleet drew a choice's name or words on (its words' rows belong to
    the name above them), from the first name to the answer row."""
    out, current = {}, None
    for at, line in enumerate(rows):
        name = re.search(r"│     [○▶● ] (\d)  ", line)
        if name:
            current = int(name.group(1))
        elif current is None:
            continue
        elif not re.search(r"│ {10}.*\S", line):
            break
        out[at] = current
    return out


class ReviewFix1(Base):
    """Names, the answer row and `submit` never give way to the words; the words come after (review of #1908)."""

    @classmethod
    def setUpClass(cls):
        main_editor()

    def assert_presses_match(self, rows, grid, values):
        drawn = fleet_choice_rows(rows)
        self.assertTrue(drawn)
        for at in range(len(rows)):
            pressed = [hit.action for hit in grid.hits if hit.y == at and hit.action[0] == "question.choice"]
            expected = [("question.choice", "message-response", values[drawn[at] - 1])] if at in drawn else []
            self.assertEqual(pressed, expected, (at, rows[at]))

    # 🔴3: a record without words slides exactly as main does.
    def test_a_record_without_words_slides_as_on_main(self):
        old = main_editor()
        eight = tuple((f"선지 {n}", "") for n in range(1, 9))
        folded = tuple((f"이름이 길어서 접히는 {n}번째 선지 " + "가나다라마바사아자차카타" * 2, "") for n in range(1, 7))
        for options in (eight, folded):
            self.fresh()
            envelope = self.native(options=options)
            for rows in (12, 13):                       # an odd and an even number of choice rows fit
                for step in range(len(options)):
                    with self.subTest(options=len(options), rows=rows, step=step):
                        keys = [DOWN] * step
                        self.assertEqual(popup(envelope, cols=64, rows=rows, keys=keys)["lines"],
                                         popup(envelope, cols=64, rows=rows, keys=keys, source=old)["lines"])
        self.fresh()
        envelope = self.native(options=eight)
        self.assertEqual(shown_choices(popup(envelope, cols=64, rows=12, keys=[DOWN] * 4)["lines"]), [3, 4, 5, 6])

    def test_fleet_keeps_every_name_the_answer_and_submit(self):
        envelope = self.native(options=EIGHT)
        values = [label.replace(" (권장)", "") for label, _ in EIGHT]
        rows, grid, *_ = fleet(envelope, cols=100, height=30, mark=(7, True))
        text = "\n".join(rows)
        for n, value in enumerate(values, 1):
            self.assertRegex(text, r"[○▶] %d  %s" % (n, value))
        self.assertIn("▶ 8  수정", text)
        self.assertIn("│     > …", text)
        for said in EIGHT[7][1].split("\n"):                            # the chosen choice's words first
            self.assertIn("│          " + said, text)
        self.assert_presses_match(rows, grid, values)
        # At every height the names, the answer row and `submit` the same question without words shows (main's
        # drawing: no words, no change) stand with the words too.
        self.fresh()
        plain = self.native(options=tuple((label, "") for label, _ in EIGHT))
        essential = lambda rows: [line[line.index("│"):].rstrip() for line in rows
                                  if re.search(r"│     ([○▶] \d  |> |submit)", line)]
        for height in range(14, 36, 2):
            with self.subTest(height=height):
                kept = essential(fleet(plain, cols=100, height=height, mark=(7, True), whole=True)[0])
                rows, grid, *_ = fleet(envelope, cols=100, height=height, mark=(7, True), whole=True)
                self.assertEqual(essential(rows), kept)
                self.assert_presses_match(rows, grid, values)
        long = "\n".join(f"긴 설명 {n}번째 줄" for n in range(1, 42))
        self.fresh()
        options = (("그림과 이름", long), OPTIONS[1], OPTIONS[2], OPTIONS[3])
        envelope = self.native(options=options)
        values = [label for label, _ in options]
        for height in (18, 24):
            with self.subTest(height=height):
                rows, grid, *_ = fleet(envelope, cols=100, height=height)
                text = "\n".join(rows)
                for n, value in enumerate(values, 1):
                    self.assertRegex(text, r"○ %d  %s" % (n, value))
                self.assertIn("│     > …", text)
                cut = [line for line in rows if "긴 설명" in line]
                if cut:
                    self.assertTrue(cut[-1].rstrip().endswith("…"), cut[-1])
                self.assert_presses_match(rows, grid, values)

    # 🔴4: words that begin like the answer row are no editor; the cursor stands where it always did.
    def test_words_that_look_like_the_answer_row_get_no_cursor(self):
        self.assertIn("const edit=part=>block.editPart(part);", (SHELL / "host.cjs").read_text())
        envelope = self.native(options=(("가", "› 답처럼 생긴 설명입니다"), ("나", "")))
        self.assertIsNone(popup(envelope, cols=64)["cursor"])
        self.assertIsNone(popup(envelope, cols=64, keys=[DOWN])["cursor"])
        old = main_editor()
        self.fresh()
        plain = self.native()
        for keys in ([], [{"write": True}], ["앞에는"], ["앞에는", "\n", "뒤에는"], ["4", "가나"], ["가나", "\x1b[A"]):
            with self.subTest(keys=keys):
                self.assertEqual(popup(plain, cols=64, keys=keys)["cursor"],
                                 popup(plain, cols=64, keys=keys, source=old)["cursor"])
        self.assertIsNotNone(popup(plain, cols=64, keys=["가나", "\x1b[A"])["cursor"])   # a draft row, not live

    # 🔴5: a frame option's own description first, once, on the card and on the board (with the scope line).
    def test_a_frame_option_with_a_description_and_means_shows_the_description_once(self):
        import frame_interview as FI
        interview = {"understanding": "검토 범위를 정합니다.", "questions": [{"id": "q-scope", "question": "어디까지 볼까요?",
                     "options": [{"label": "첫 장만", "description": "먼저 읽어야 하는 다른 설명.",
                                  "means": "첫 장의 그림과 이름을 대조합니다."},
                                 {"label": "도감 전체", "means": "모든 장에서 같은 표기법을 확인합니다."}]}],
                     "route_proposals": {"question": "q-scope", "by_option": {
                         "첫 장만": {"legs": [{"capability": "a"}, {"capability": "b"}], "execution_scope": "complete"}}}}
        with mock.patch.dict(os.environ, {"FLEET_NOW_LANG": "en"}):
            note = FI.route_scope_note(interview, "q-scope", "첫 장만")
            card = FI.pending_question_block(interview)
            question = interview["questions"][0]
            choices = [HWS._frame_gate_choice(interview, question, option) for option in question["options"]]
        self.assertTrue(note)
        self.assertEqual(card.split("\n")[3:], ["- 첫 장만", "  먼저 읽어야 하는 다른 설명.", "  " + note,
                                                 "- 도감 전체", "  모든 장에서 같은 표기법을 확인합니다."])
        self.assertEqual(HWS.choice_descriptions({"choices": choices}),
                         ["먼저 읽어야 하는 다른 설명.\n" + note, "모든 장에서 같은 표기법을 확인합니다."])
        frame = {"kind": "question", "state": "open", "request_id": "S2:fixture", "native_ref": {"transport": "workflow-gate"},
                 "detail": {"questions": [{"id": "q-scope", "question": "어디까지 볼까요?", "is_other": True,
                                           "choices": choices}]}}
        text = "\n".join(popup(frame, cols=108)["lines"])
        self.assertIn("│      먼저 읽어야 하는 다른 설명.", text)
        self.assertIn("│      " + note[:40], text)
        self.assertNotIn("첫 장의 그림과 이름을 대조합니다.", text)

    # ---- the five changes undone, each failing its guard above ----
    def mutated(self, old, new):
        source = (SHELL / "block-input.cjs").read_text()
        cut = source.replace(old, new)
        self.assertNotEqual(cut, source)
        return cut

    def test_undone_fleet_words_push_submit_out(self):
        from tools.fleet.explorer import tables
        with mock.patch.object(tables, "_fit_choice_words", lambda details, room, lit, width: details):
            with self.assertRaises(StopIteration):                     # no `submit` row is drawn
                fleet(self.native(options=EIGHT), cols=100, height=30, mark=(7, True))

    def test_undone_the_editor_row_chosen_by_its_look(self):
        cut = self.mutated("part.role!=='choice-detail'&&String(part.text)", "String(part.text)")
        envelope = self.native(options=(("가", "› 답처럼 생긴 설명입니다"), ("나", "")))
        self.assertIsNotNone(popup(envelope, cols=64, source=cut)["cursor"])


# ---- review 2 of #1908 (head 524542cfe): the words take only the rows left empty without them ----
# One rule: lay the question out as without words (main's drawing), then the rows it could still grow by take the
# words. So: the drawing with the words' rows struck out is, row by row, the drawing of the same question without
# words. These sweep both drawings over sizes, choices, words, the highlighted place and the answer's states.
SWEEP = r"""
const dir=process.argv[1],a=JSON.parse(require('node:fs').readFileSync(0,'utf8')),Module=require('node:module');
let create=require(dir+'/block-input.cjs');
if(a.source){const m=new Module(dir+'/block-input.cjs');m.filename=dir+'/block-input.cjs';m.paths=module.paths;m._compile(a.source,m.filename);create=m.exports;}
const view=require(dir+'/panel-view.cjs');
const settle=async b=>{for(let i=0;i<400&&!b.stalled;i++)await new Promise(r=>setTimeout(r,1));};
async function draw(items,labels,cols,rows,keys,mode){
  const b=create({changed(){},action(){},answer:async()=>({state:'submitted'}),pasteIdle:3});
  b.update(items,'',{labels});b.enter();
  for(const key of keys)b.input(Buffer.from(key,'utf8'));
  if(mode==='edit')b.input(Buffer.from('앞에는 이름만\n그림은 뒷장에','utf8'));
  if(mode==='paste'||mode==='stalled')b.input(Buffer.from('\x1b[200~'+['하나','둘','셋','넷','다섯','여섯','일곱'].join('\r'),'utf8'));
  if(mode==='stalled')await settle(b);
  const width=view.popupWidth(cols,'question'),inner=Math.max(0,width-4),room=view.popupRoom(rows,1);
  const all=b.detail({budget:room+1,columns:inner}),body=all.slice(1);
  const v=view.popup({cols,rows,band:1,kind:'question',title:all[0],body,sections:['NOW','QUESTIONS'],current:1,model:{}});
  const top=v.frame.top-v.top,left=v.frame.left-v.left;
  const lines=v.rows.slice(top,top+v.frame.height).map(r=>r.map(p=>p.text).join('').slice(left).replace(/\s+$/,''));
  const words=body.slice(0,v.body).map((r,i)=>r.some(p=>p.role==='choice-detail')?i+1:-1).filter(i=>i>=0);
  const hits=v.hits.map(h=>[h.row-v.frame.top,h.action]);
  return {lines,words,hits,stalled:b.stalled,pasting:b.pasting};
}
(async()=>{const out=[];
for(const c of a.cases){
  const [plain,given]=await Promise.all([draw(a.plain,a.labels,a.cols,c.rows,c.keys,c.mode),draw(a.items,a.labels,a.cols,c.rows,c.keys,c.mode)]);
  const kept=given.lines.filter((_,i)=>!given.words.includes(i));
  const map=given.lines.map((_,i)=>i).filter(i=>!given.words.includes(i));
  const hitsKept=given.hits.filter(([r])=>!given.words.includes(r)).map(([r,act])=>[map.indexOf(r),act]);
  const same=JSON.stringify(kept)===JSON.stringify(plain.lines)&&JSON.stringify(hitsKept)===JSON.stringify(plain.hits);
  out.push({...c,same,words:given.words.length,plain:same&&!a.keep?undefined:plain.lines,given:same&&!a.keep?undefined:given.lines,stalled:given.stalled});
}
process.stdout.write(JSON.stringify(out));})().catch(e=>{console.error(e);process.exit(1)});
"""


def message_envelope(options, *, described=True, later=False):
    """A registered question's record as the board reads it (synthetic; the read boundary is the real one)."""
    question = {"id": "message-response", "question": QUESTION, "choices": [label for label, _ in options],
                "recommended": 0, "is_secret": False, "is_other": True, "required": True}
    if described and any(words for _label, words in options):
        question["choice_descriptions"] = {label: words for label, words in options if words}
    listed = [question] + ([{"id": "second", "question": "두 번째 질문도 고릅니다.", "choices": ["예", "아니요"],
                             "is_other": True}] if later else [])
    return {"kind": "question", "state": "open", "harness": "claude", "request_id": "S4:sweep",
            "question_id": "message-response", "detail": {"questions": listed}, "support": {"supported": True},
            "native_ref": {"transport": "hearting-message-answer"}}


def popup_sweep(width, count, lines, rows=range(10, 31), modes=("choose", "edit", "paste", "stalled"), source=None,
                keep=False):
    """Each case's drawing with its words' rows struck out against the same question without words."""
    from session_shell import block_labels
    options = tuple((f"선지 {n}", "\n".join(f"{n}번 설명 {k}째 줄입니다." for k in range(1, lines + 1)))
                    for n in range(1, count + 1))
    with mock.patch.dict(os.environ, {"FLEET_NOW_LANG": "en"}):
        labels = block_labels(width)
    items = board_items(message_envelope(options), popup=width)
    plain = board_items(message_envelope(options, described=False), popup=width)
    cases = [{"rows": r, "keys": [DOWN] * step, "mode": mode} for r in rows
             for step in sorted({0, count - 1}) for mode in modes]
    args = {"items": items, "plain": plain, "labels": labels, "cols": width + 4, "cases": cases, "keep": keep,
            **({"source": source} if source else {})}
    done = subprocess.run([NODE, "-e", SWEEP, str(SHELL)], input=json.dumps(args, ensure_ascii=False),
                          capture_output=True, text=True, timeout=300)
    if done.returncode:
        raise AssertionError(done.stdout + done.stderr)
    return json.loads(done.stdout)


def fleet_words_rows(rows):
    """The rows Fleet drew a choice's words on (from its name's first cell, ten cells in from the pane's line)."""
    at = rows[3].index("┌")
    return [y for y, line in enumerate(rows) if re.match(r"│ {10,}\S", line[at:])]


def fleet_sweep_case(envelope, plain, *, cols, height, mark, extra):
    """Fleet's drawing with its words' rows struck out against the same waits without words: the pane's rows of
    the page body and the presses on them, the header and the footer whole."""
    words_rows, sides = None, []
    for given in (envelope, plain):
        rows, grid, *_ = fleet(given, cols=cols, height=height, mark=mark, whole=True, extra=extra)
        at = rows[3].index("┌")
        words_rows = fleet_words_rows(rows) if given is envelope else []
        body = [y for y in range(4, height - 4) if y not in words_rows]
        pane = [rows[y][at:].rstrip() for y in body]
        presses = [[(hit.x0, hit.x1, hit.action) for hit in grid.hits if hit.y == y and hit.x1 > at] for y in body]
        outside = [rows[y] for y in range(height) if not 4 <= y < height - 4]
        sides.append((pane, presses, outside, words_rows, rows, grid))
    (pane, presses, outside, words_rows, rows, grid), (pane0, presses0, outside0, *_rest) = sides
    blank = len(pane0) - len(pane)
    same = (pane == pane0[:len(pane)] and all(line == "│" for line in pane0[len(pane):])
            and presses == presses0[:len(pane)] and not any(presses0[len(pane):]) and outside == outside0)
    return same, words_rows, rows, grid, blank


class ReviewFix2(Base):
    """The words take only the rows the question would leave empty without them (review 2 of #1908)."""

    @classmethod
    def setUpClass(cls):
        main_editor()

    def test_the_popup_with_its_words_struck_out_is_the_popup_without_words(self):
        total = differ = 0
        drawn_words = 0
        for width in (36, 40, 60, 104):
            for count in range(1, 9):
                for lines in (1, 4, 21):
                    for case in popup_sweep(width, count, lines):
                        total += 1
                        drawn_words += case["words"]
                        if not case["same"]:
                            differ += 1
                            self.fail("width %d · %d choices · %d lines · %s\n%s\n--- without words ---\n%s" % (
                                width, count, lines, {k: case[k] for k in ("rows", "keys", "mode")},
                                "\n".join(case["given"]), "\n".join(case["plain"])))
        self.assertEqual((total, differ), (4 * 8 * 3 * 21 * 4 * 2 - 4 * 3 * 21 * 4, 0))
        self.assertGreater(drawn_words, 0)

    def test_fleet_with_its_words_struck_out_is_fleet_without_words(self):
        total = 0
        for words, mark in ((EIGHT, (7, True)), ((("그림과 이름", "\n".join(f"긴 설명 {n}번째 줄" for n in range(1, 42))),
                                                  *OPTIONS[1:]), None)):
            for later in (False, True):
                given, plain = (message_envelope(words, later=later),
                                message_envelope(words, described=False, later=later))
                for extra in ({}, {"owner_state": "successor", "registered": "[b6]"},
                              {"session_tag": "", "owner_sid": "abc12345"}):
                    for cols in (80, 100, 132):
                        for height in range(14, 35):
                            with self.subTest(later=later, extra=extra, cols=cols, height=height, words=len(words)):
                                total += 1
                                same, words_rows, rows, grid, _blank = fleet_sweep_case(
                                    given, plain, cols=cols, height=height, mark=mark, extra=extra)
                                self.assertTrue(same, "\n".join(rows))
                                # Each row of words presses its own choice.
                                for y in words_rows:
                                    name = next(int(m.group(1)) for line in rows[y::-1]
                                                if (m := re.search(r"│     [○▶● ] (\d)  ", line)))
                                    self.assertEqual([hit.action for hit in grid.hits
                                                      if hit.y == y and hit.x1 > rows[3].index("┌")],
                                                     [("question.choice", "message-response",
                                                       [label for label, _ in words][name - 1])])
        self.assertEqual(total, 2 * 2 * 3 * 3 * 21)

    # Review 2 🔴1: Fleet with a session note: `submit` (and its press) stays.
    def test_fleet_keeps_submit_beside_a_session_note(self):
        for extra, said in (({"owner_state": "successor", "registered": "[b6]"}, "taken over"),
                            ({"session_tag": "", "owner_sid": "abc12345"}, "not in the Fleet list")):
            for later in (False, True):
                with self.subTest(said=said, later=later):
                    same, words_rows, rows, grid, _blank = fleet_sweep_case(
                        message_envelope(EIGHT, later=later), message_envelope(EIGHT, described=False, later=later),
                        cols=100, height=30, mark=(7, True), extra=extra)
                    self.assertTrue(same, "\n".join(rows))
                    self.assertIn(said, "\n".join(rows))
                    at = next(y for y, line in enumerate(rows) if "│     submit" in line)
                    self.assertEqual([hit.action for hit in grid.hits if hit.y == at and hit.x1 > rows[3].index("┌")],
                                     [] if later else [("question.submit",)])
                    # Rows left without words take them (the chosen choice's first); with a later question none are.
                    self.assertEqual(bool(words_rows), not later)

    # Review 2 🔴2: `+N` stands exactly where it stands without words.
    def test_more_choices_stands_as_without_words(self):
        two = (("첫째", "\n".join(f"설명 {n}" for n in range(1, 5))), ("둘째", ""))
        lines = popup(self.native(options=two), cols=64, rows=12)["lines"]
        self.assertEqual(shown_choices(lines), [1, 2])                 # nothing hidden, no `+N`
        self.fresh()
        lines = popup(self.native(options=EIGHT), cols=64, rows=12, keys=[DOWN] * 4)["lines"]
        self.assertTrue([line for line in lines if re.match(r"│ \+\d · more choices · ↑↓", line)], lines)
        self.assertEqual(shown_choices(lines), [3, 4, 5, 6])

    # Review 2 🔴3: the paste notices (#1871) stand as without words.
    def test_the_paste_notices_stand_as_without_words(self):
        for rows, lines in ((14, 4), (30, 21)):
            for mode, said in (("paste", "pasting · Enter sends once it stops"),
                               ("stalled", "paste stalled · Enter send · Esc/Ctrl+] back")):
                with self.subTest(rows=rows, mode=mode):
                    for case in popup_sweep(60, 2, lines, rows=(rows,), modes=(mode,), keep=True):
                        self.assertTrue(case["same"])
                        text = "\n".join(case["given"])
                        self.assertIn(said, text)
                        if rows == 30:                                # tall enough for the answer's first row
                            self.assertIn("│ › 하나", text)
                        self.assertEqual(case["stalled"], mode == "stalled")

    # Review 2 🔴4: a scope line already in the description shows once, on the card and the board.
    def test_a_scope_line_already_in_the_words_shows_once(self):
        import frame_interview as FI
        def interview(description=None):
            option = {"label": "첫 장만", "means": "첫 장의 그림과 이름을 대조합니다."}
            if description is not None:
                option["description"] = description
            return {"understanding": "검토 범위를 정합니다.", "questions": [{"id": "q-scope", "question": "어디까지 볼까요?",
                    "options": [option, {"label": "도감 전체", "means": "모든 장에서 같은 표기법을 확인합니다."}]}],
                    "route_proposals": {"question": "q-scope", "by_option": {
                        "첫 장만": {"legs": [{"capability": "a"}, {"capability": "b"}], "execution_scope": "complete"}}}}
        for lang in ("en", "ko"):
            with mock.patch.dict(os.environ, {"FLEET_NOW_LANG": lang}):
                note = FI.route_scope_note(interview(), "q-scope", "첫 장만")
                for description in ("먼저 읽어야 하는 다른 설명.\n" + note, None, "   "):
                    with self.subTest(lang=lang, description=description):
                        given = interview(description)
                        card = FI.pending_question_block(given)
                        question = given["questions"][0]
                        choices = [HWS._frame_gate_choice(given, question, option) for option in question["options"]]
                        board = HWS.choice_descriptions({"choices": choices})[0]
                        self.assertEqual(card.count(note), 1, card)
                        self.assertEqual(board.count(note), 1, board)
                        if description and description.strip():
                            self.assertEqual(card.split("\n")[4:6], ["  먼저 읽어야 하는 다른 설명.", "  " + note])
                            self.assertEqual(board, "먼저 읽어야 하는 다른 설명.\n" + note)
                        else:
                            self.assertEqual(card.split("\n")[4:6], ["  첫 장의 그림과 이름을 대조합니다.", "  " + note])

    # ---- the rule undone, each failing its guard above ----
    def test_undone_the_words_laid_out_first(self):
        cut = (SHELL / "block-input.cjs").read_text().replace(
            "const marked=full.filter(parts=>!parts.some(part=>part.role==='choice-detail'));", "const marked=full;")
        self.assertNotIn("full.filter(parts=>", cut)
        cases = popup_sweep(60, 3, 4, rows=range(10, 15), modes=("choose",), source=cut)
        self.assertFalse(all(case["same"] for case in cases))
        from tools.fleet.explorer import tables
        with mock.patch.object(tables, "_choice_words_room", lambda *a, **k: 99):
            same, *_ = fleet_sweep_case(message_envelope(EIGHT), message_envelope(EIGHT, described=False),
                                        cols=100, height=24, mark=(7, True), extra={})
        self.assertFalse(same)

    def test_undone_more_choices_dropped(self):
        source = (SHELL / "block-input.cjs").read_text()
        cut = source.replace("...(spare>=2?[[span(`+${marked.length-size} · ", "...(false?[[span(`+${marked.length-size} · ")
        self.assertNotEqual(cut, source)
        lines = popup(self.native(options=EIGHT), cols=64, rows=12, keys=[DOWN] * 4, source=cut)["lines"]
        self.assertFalse([line for line in lines if "more choices" in line])

    def test_undone_a_scope_line_twice(self):
        import inspect
        import textwrap
        source = textwrap.dedent(inspect.getsource(HWS.choice_descriptions))
        cut = source.replace("if note and note not in \" \".join(said.split()):", "if note:")
        self.assertNotEqual(cut, source)
        space = dict(vars(HWS))
        exec(compile(cut, HWS.__file__, "exec"), space)
        with mock.patch.object(HWS, "choice_descriptions", space["choice_descriptions"]):
            import frame_interview as FI
            with mock.patch.dict(os.environ, {"FLEET_NOW_LANG": "en"}):
                choice = {"description": "다른 설명.", "scope_note": "Runs all 2 legs."}
                choice["description"] += "\n" + choice["scope_note"]
                self.assertEqual(HWS.choice_descriptions({"choices": [choice]})[0].count("Runs all 2 legs."), 2)


if __name__ == "__main__":
    unittest.main()
