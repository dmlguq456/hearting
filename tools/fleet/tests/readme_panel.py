"""Public README demo for the session panel: English, neutral, synthetic.

The panel of one ``readme_demo`` session, drawn off-screen. Its rows come from the shell
backend's own projection (``utilities/session_shell.py`` ``panel_projection``) over the README
demo world: the route rows are the ones Fleet's session line reads from the same
``readme_demo.snapshot()`` (``session_route_status``), so the panel head and Fleet's row for the
same session show the same values. Only the backend's readers of live state (the native session,
answer waits, prompt record, message ledger, request records, memory journal, open work) are
replaced by the demo values below; the LAB section reads a demo experiment board written by the
real catalog and run-record writers into the temporary HOME. The panel is painted by
``utilities/session-shell/panel-view.cjs`` under node, with the question editor
``block-input.cjs`` for focus and answers.

Nothing here reads the real home directory, a registry, herdr, tmux or the network; no live
window, session or model is started. ``readme_captures.py`` draws the published images from it.
"""
import contextlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[3]
for _path in (str(ROOT), str(ROOT / "tools"), str(ROOT / "utilities")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

PANEL_VIEW = ROOT / "utilities" / "session-shell" / "panel-view.cjs"
BLOCK_INPUT = ROOT / "utilities" / "session-shell" / "block-input.cjs"

# Sessions of the demo world the panel images show (``readme_demo.snapshot()`` rows).
API, API_REVIEW, API_NOTES, ML = ("readme-claude-api", "readme-codex-api", "readme-claude-notes",
                                  "readme-claude-ml")


def _ago(now, minutes):
    return now - minutes * 60


def _iso(epoch):
    return time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(epoch))


def _received(now):
    """What each demo session's own prompt record kept: peer messages it received."""
    return {
        API: [{"source": "peer", "sender": "Codex [b2]", "sender_harness": "codex",
               "sender_session_id": API_REVIEW, "kind": "reply", "at": _ago(now, 19),
               "text": "Pagination review done: 2 findings, both in cursor.py; details in the report."}],
        ML: [{"source": "peer", "sender": "OpenCode [e8]", "sender_harness": "opencode",
              "sender_session_id": "readme-oc-ml", "kind": "reply", "at": _ago(now, 6),
              "text": "8 loader workers is the knee: 1.9x over 4, flat after that."}],
    }


def _sent(now):
    """Each demo session's own sent-message ledger rows (``_sent_peer_messages``)."""
    return {
        API: [{"to": {"harness": "claude", "session_id": API_NOTES, "name": "web-api-9c"},
               "summary": "The release notes can wait for the limiter; I will ping you.",
               "at": _ago(now, 8), "message_id": "m-demo-1", "kind": "steer",
               "status": "received", "refs": []}],
        ML: [{"to": {"harness": "opencode", "session_id": "readme-oc-ml", "name": "ml-train-e8"},
              "summary": "Profile 4, 8 and 16 loader workers and tell me where it flattens.",
              "at": _ago(now, 14), "message_id": "m-demo-2", "kind": "steer",
              "status": "received", "refs": []}],
    }


def _request(rid, title, state, requester, recipient, updated, revisions, reports=(), body=""):
    return {"id": rid, "title": title, "state": state, "updated": updated, "cancel": None,
            "requester": requester, "recipient": recipient, "revisions": revisions,
            "reports": list(reports), "body": body}


def _requests(now):
    """The request records each demo session is a party to (``_request_views``)."""
    four_f = {"harness": "claude", "session_id": API, "name": "web-api-4f"}
    b2 = {"harness": "codex", "session_id": API_REVIEW, "name": "web-api-b2"}
    tests = _request(
        "rq-7d21c4", "API tests on feat/rate-limit", "partial", four_f, b2, _ago(now, 2),
        [{"n": 1, "label": "", "state": "current", "transfer_ref": "t-demo-1"}],
        [{"n": 1, "kind": "partial", "revision": 1, "at": _ago(now, 2), "transfer_ref": "t-demo-2",
          "result": "31 of 40 suites pass; 9 still running", "remaining": "9 suites"}],
        body="Re-run the API test suites on feat/rate-limit and report the result.\n"
             "A partial report is fine while suites are still running.")
    review = _request(
        "rq-3a90e1", "Review the pagination fix", "final", four_f, b2, _ago(now, 19),
        [{"n": 1, "label": "", "state": "current", "transfer_ref": "t-demo-3"}],
        [{"n": 1, "kind": "final", "revision": 1, "at": _ago(now, 19), "transfer_ref": "t-demo-4",
          "result": "2 findings, both in cursor.py"}],
        body="Review the pagination off-by-one fix and report findings.")
    seen = {"t-demo-1": {"state": "received"}, "t-demo-2": {"state": "received"},
            "t-demo-3": {"state": "received"}, "t-demo-4": {"state": "queued"}}
    out = [{"record": tests, "side": "out", "rows": seen, "gone": False, "body": tests["body"]},
           {"record": review, "side": "out", "rows": seen, "gone": False, "body": review["body"]}]
    inbound = [{"record": tests, "side": "in", "rows": seen, "gone": False, "body": tests["body"]}]
    return {API: out, API_REVIEW: inbound}


def _questions(now):
    """The answer-wait board of the demo session that waits on you (``[9c]``)."""
    owner = {"harness": "claude", "session_id": API_NOTES}
    return {API_NOTES: [
        {"id": "q-demo-layout", "revision": "r1", "request_state": "open", "answerable": True,
         "answer_kind": "message", "owner": owner, "since": _ago(now, 7),
         "questions": [{"id": "layout", "question": "Which layout for the 2.4 notes?",
                        "choices": ["Per release", "Per component", "Keep 2.3's"],
                        "recommended": 0}]},
        {"id": "q-demo-scope", "revision": "r1", "request_state": "open", "answerable": True,
         "answer_kind": "message", "owner": owner, "since": _ago(now, 5),
         "questions": [{"id": "scope", "question": "Include internal API changes?",
                        "choices": ["Public only", "Public and internal"],
                        "recommended": 0}]},
    ]}


def _memory(clock):
    """The newest saved memory records of each demo project (``recent_memory``). The panel counts
    a record's age from the wall clock, so ``clock`` is the capture's own real time."""
    return {
        "web-api": [{"type": "decision", "ts": _iso(_ago(clock, 41)), "headline_id": None,
                     "headline": "", "snippet": "The limit lives in the gateway, per API key."}],
        "ml-train": [{"type": "decision", "ts": _iso(_ago(clock, 180)), "headline_id": None,
                      "headline": "", "snippet": "Evaluate on the 16 kHz subset first."}],
    }


def _git(worktrees):
    """A clean Git observation: no operation under way, up to date, ``worktrees`` linked ones."""
    return {"repo": 1, "operation": "none", "dirty": 0, "ahead": 0, "behind": 0,
            "extra_worktrees": worktrees}


def write_lab_board(project, now):
    """A demo experiment board for ``project``, written by the real catalog and run writers."""
    import artifact_admission as admission
    import experiment_catalog as catalog
    import resource_progress as progress
    root = Path(project) / ".agent_reports"
    root.mkdir(parents=True, exist_ok=True)
    admission.ensure_root_identity(root)
    metric = {"key": "wer", "unit": "%", "direction": "min", "split": "validation",
              "scoring": "16 kHz subset"}
    spec = {"groups": [{"display_name": "Checkpoint eval", "status": "running",
                        "goal": "Does the new checkpoint beat 12.4% WER on the 16 kHz subset?",
                        "metric": dict(metric),
                        "target": {"kind": "fixed", "label": "baseline", "value": 12.4,
                                   "split": "validation", "scoring": "16 kHz subset"}}],
            "conditions": [{"display_name": name, "group": "Checkpoint eval",
                            "refs": {"runs": [{"run_id": run}]}}
                           for name, run in (("beam 4", "run-beam4"), ("beam 8", "run-beam8"),
                                             ("last ckpt", "run-last"))]}
    catalog.run_write(root, lambda ctx: catalog.op_register(ctx, spec), now=now - 3600)
    healthy = {"state": "ok", "seconds_since_anchor": 60.0}
    for run, points, status in (("run-beam4", [(10, 12.9), (12, 11.8)], "running"),
                                ("run-beam8", [(10, 13.1), (12, 12.2)], "running"),
                                ("run-last", [(10, 13.4), (12, 13.0)], "running")):
        samples = [{"split": "validation", "epoch": epoch, "metrics": {"wer": value}}
                   for epoch, value in points]
        summary = progress.metric_summary(samples, decision_metric="wer", direction="min")
        observation = {"reached": 12, "total": 40, "unit": "epoch", "eta_seconds": 1800.0,
                       "at": now - 180, "summary": summary, "stagnation": healthy}
        record = progress.public_run_record(
            {"run_id": run, "status": status, "started_at": now - 3600, "decision_metric": "wer"},
            status=status, observation=observation, metrics=[], started_at=now - 3600, ended_at=None)
        progress.write_public_run_record(root, run, record)
    return root


class World:
    """The demo world's panel inputs, keyed by native session id."""

    def __init__(self, snapshot, now):
        from tools.fleet.session_line import session_route_status
        self.snapshot, self.now = snapshot, now
        self.sessions = {s.session_id: s for s in snapshot.sessions if not s.is_child}
        self.tags = {(s.harness, s.session_id): "[" + s.session_tag + "]"
                     for s in self.sessions.values() if s.session_tag}
        with mock.patch("time.time", return_value=now):
            self.labels, self.panels = session_route_status(snapshot, include_details=True)
        self.received, self.sent = _received(now), _sent(now)
        self.requests, self.questions, self.memory = _requests(now), _questions(now), _memory(time.time())
        # The LAB board of the project that runs experiments, in the temporary HOME.
        home = Path(os.environ["HOME"])
        self.lab_roots = {"ml-train": write_lab_board(home / "demo-projects" / "ml-train", now)}

    def tag(self, harness, sid):
        return self.tags.get((harness, sid), "")

    def values(self, harness, sid):
        s = self.sessions[sid]
        quota = [("5h", 18, None), ("7d", 40, None)] if harness == "claude" else (
            [("5h", s.rl_5h, None), ("7d", s.rl_7d, None)] if s.rl_5h is not None else [])
        return {"cwd": s.cwd, "model": s.model, "effort": s.effort, "ctx_pct": s.ctx_pct,
                "active_context_tokens": None, "context_window_tokens": None,
                "compaction_pressure": None, "harness": harness, "title": s.title,
                "now": s.summary or None, "native_turn": None,
                "git": _git(1 if any(j.parent_sid == sid for j in self.snapshot.jobs) else 0),
                "branch": s.branch, "weekly_pct": quota[1][1] if len(quota) > 1 else None,
                "quota_windows": quota, "model_quotas": [], "jobs": None}

    def view(self):
        waiting = {}
        for sid, items in self.questions.items():
            harness = self.sessions[sid].harness
            waiting[(harness, sid)] = [item["id"] for item in items]
        return {"route_panels": self.panels, "by_session": waiting,
                "items": [item for items in self.questions.values() for item in items]}

    def counts(self, harness, sid):
        mine = len(self.questions.get(sid, ()))
        return (sum(len(v) for v in self.questions.values()), mine)

    def display(self, sid):
        return {"received": self.received.get(sid, []), "human_state": "none", "human": None}


def projection(world, harness, sid, *, columns=40, rows=46, state="working"):
    """``panel_projection``'s payload for one demo session, its live readers replaced."""
    import human_wait_state
    import session_shell
    import session_tidy
    from fleet import session_lab, session_tags, session_view, session_work
    from fleet.collectors import steward
    now = world.now

    def observe(h, pid, start, s, **_):
        return world.values(h, s), []

    def memory(cwd):
        return [dict(row) for row in world.memory.get(os.path.basename(str(cwd or "")), [])], []

    real_lab_read = session_lab.read

    def lab_read(cwd, now=None):
        # Only the demo project that runs experiments has a board (written into the temporary HOME).
        board = world.lab_roots.get(os.path.basename(str(cwd or "")))
        return real_lab_read(str(board.parent), now=now) if board else (None, [])

    patches = [
        mock.patch("time.time", return_value=now),
        mock.patch.object(human_wait_state, "cached_statusline",
                          lambda h, s: (world.counts(h, s), world.labels.get(h + "\0" + s, ""))),
        mock.patch.object(human_wait_state, "cached_view", lambda request=False: world.view()),
        mock.patch.object(session_tidy, "resolve_seat", lambda *a, **k: None),
        mock.patch.object(session_tidy, "prompt_display", lambda seat, h, s, **k: world.display(s)),
        mock.patch.object(session_tidy, "waiting_notice_display", lambda *a, **k: ("", [])),
        mock.patch.object(session_tidy, "session_seat", lambda *a, **k: None),
        mock.patch.object(session_tidy, "_prompt_seq_path", lambda *a, **k: Path("/nonexistent/seq")),
        mock.patch.object(session_tags, "assigned_tag", lambda h, s: world.tag(h, s)),
        mock.patch.object(session_view, "observe", observe),
        mock.patch.object(steward, "role_flag", lambda h, s, cwd=None: bool(world.sessions[s].steward)),
        mock.patch.object(session_work, "recent_memory", memory),
        mock.patch.object(session_work, "observe", lambda cwd, ids=(): (session_work.WorkRows(), [])),
        mock.patch.object(session_shell, "_sent_peer_messages",
                          lambda s, limit=8: ([dict(row) for row in world.sent.get(s, [])], [])),
        mock.patch.object(session_shell, "_request_views",
                          lambda h, s, n, errors=None: (json.loads(json.dumps(world.requests.get(s, []))), [])),
        mock.patch.object(session_shell, "session_questions",
                          lambda h, s, view=None, retained=(): json.loads(json.dumps(world.questions.get(s, [])))),
        mock.patch.object(session_lab, "read", lab_read),
        mock.patch.dict(os.environ, {"TERM": "xterm-256color"}),
    ]
    with contextlib.ExitStack() as stack:
        for patch in patches:
            stack.enter_context(patch)
        os.environ.pop("COLORTERM", None)
        os.environ.pop("NO_COLOR", None)
        session_lab.clear_cache()
        payload = session_shell.panel_projection(harness, sid, native_pid=81000 + len(sid),
                                                 native_start=1, columns=columns, rows=rows,
                                                 state=state, expanded=True)
    return payload


# Paints one or more panel frames with the real renderer and question editor; prints the rows.
_NODE = r"""
const fs=require('node:fs');
const [viewPath,blockPath]=process.argv.slice(1);
const view=require(viewPath),create=require(blockPath);
const tick=()=>new Promise(resolve=>setImmediate(resolve));
(async()=>{
  const jobs=JSON.parse(fs.readFileSync(0,'utf8')),out=[];
  for(const job of jobs){
    const p=job.payload,sent=[];
    const block=create({changed(){},answer:async v=>{sent.push(v);return {state:'received'};},action(){},page(){}});
    block.update(p.questions||[],p.help||'',p.block||{});
    for(const key of job.keys||[]){
      if(key==='enter')block.enter();else if(key==='leave')block.leave();else block.input(Buffer.from(key));
      await tick();await tick();
    }
    const model={...p.panel,answerable:(p.questions||[]).some(i=>i.answerable===true&&(i.request_state||'open')==='open')};
    const r=view.render({width:job.width,height:job.height,side:job.side,fill:job.side,model,
      native:p.native_display||{},state:job.state,sid:job.sid,harness:job.harness,frame:job.frame||0,since:job.since,
      sections:p.block,configuration:job.configuration||{},expandedKeys:job.expanded||[],focused:block.focused,
      cwd:'',background:'dark',questions:o=>block.detail(o)});
    out.push(r.rows.map(row=>row.map(part=>({text:part.text,fg:part.foreground??null,bg:part.background??null,
      dim:part.dim===true,bold:part.bold===true,reverse:part.reverse===true}))));
  }
  process.stdout.write(JSON.stringify(out));
})().catch(error=>{console.error(error.stack);process.exitCode=1;});
"""


def paint(jobs):
    """Rows of parts for each job (``payload``, ``width``, ``height``, ``side``, ``state`` …)."""
    result = subprocess.run(["node", "-e", _NODE, str(PANEL_VIEW), str(BLOCK_INPUT)],
                            input=json.dumps(jobs), capture_output=True, text=True, timeout=60)
    if result.returncode:
        raise RuntimeError(result.stderr.strip())
    return json.loads(result.stdout)


# --- painting -------------------------------------------------------------------------------
# The panel's parts carry xterm-256 indices (``style_components``); they are painted on the same
# cell grid, fonts and background ``explorer_capture`` paints Fleet with, and the six Fleet hues
# take the same richer RGB Fleet's capture uses, so a value reads in the same color in both.

def _xterm(index):
    base = ((0, 0, 0), (205, 0, 0), (0, 205, 0), (205, 205, 0), (0, 0, 238), (205, 0, 205),
            (0, 205, 205), (229, 229, 229), (127, 127, 127), (255, 0, 0), (0, 255, 0),
            (255, 255, 0), (92, 92, 255), (255, 0, 255), (0, 255, 255), (255, 255, 255))
    if index < 16:
        return base[index]
    if index >= 232:
        level = 8 + 10 * (index - 232)
        return (level, level, level)
    index -= 16
    steps = (0, 95, 135, 175, 215, 255)
    return (steps[index // 36], steps[(index // 6) % 6], steps[index % 6])


def _richer():
    from tools.fleet import render_cells
    return {index: tuple(round(v * 255 / 1000) for v in render_cells._RICHER_RGB_1000[name])
            for name, index in render_cells._MUTED_256.items() if name in render_cells._RICHER_RGB_1000}


def _colour(value, default, richer):
    if isinstance(value, str) and value.startswith("#") and len(value) == 7:
        return tuple(int(value[i:i + 2], 16) for i in (1, 3, 5))
    if isinstance(value, int) and 0 <= value <= 255:
        return richer.get(value) or _xterm(value)
    return default


def cell_width(char):
    """The panel's own cell rule (``panel-view.cjs`` ``measure``): above U+2E7F a glyph is wide."""
    return 2 if ord(char) > 0x2E7F else 1


def cells(rows):
    """Rows of parts as rows of ``(glyph, fg, bg, dim, bold, reverse, width)`` cells."""
    out = []
    for row in rows:
        line = []
        for part in row:
            for char in part.get("text") or "":
                if ord(char) in (0xFE0F, 0x200D):
                    continue
                line.append((char, part.get("fg"), part.get("bg"), part.get("dim"), part.get("bold"),
                             part.get("reverse"), cell_width(char)))
        out.append(line)
    return out


def draw(canvas, fonts, grid, left, top):
    """Paint ``grid`` (``cells()``) with its top-left cell at pixel ``(left, top)``."""
    from tools.fleet.tests import explorer_capture as capture
    richer = _richer()
    for y, row in enumerate(grid):
        x = 0
        for glyph, fg, bg, dim, bold, reverse, width in row:
            back = _colour(bg, capture.BACKGROUND, richer)
            fore = _colour(fg, capture.DEFAULT_FG, richer)
            if dim:
                fore = capture.blend(fore, back, 0.42)
            if reverse:
                fore, back = back, fore
            px, py = left + x * capture.CELL_W, top + y * capture.CELL_H
            canvas.rectangle([px, py, px + width * capture.CELL_W - 1, py + capture.CELL_H - 1], fill=back)
            glyph = capture.GLYPH_FALLBACK.get(glyph, glyph)
            if glyph.strip():
                family = ("wide" if width == 2 else "symbols" if "⠀" <= glyph <= "⣿"
                          else "latin") + ("-bold" if bold else "")
                font = fonts.get(family) or fonts.get("latin")
                if font is not None:
                    canvas.text((px, py - 1), glyph, font=font, fill=fore)
            x += width


def fonts():
    from PIL import ImageFont
    from tools.fleet.tests import explorer_capture as capture
    return {name: ImageFont.truetype(file, capture.FONT_PX)
            for name, file in capture.FONTS.items() if Path(file).exists()}


def image(grid, cols, rows):
    """One grid as its own picture, ``cols`` × ``rows`` cells."""
    from PIL import Image, ImageDraw
    from tools.fleet.tests import explorer_capture as capture
    picture = Image.new("RGB", (cols * capture.CELL_W, rows * capture.CELL_H), capture.BACKGROUND)
    draw(ImageDraw.Draw(picture), fonts(), grid, 0, 0)
    return picture


# --- the illustrative conversation area -------------------------------------------------------
# Plain text rows beside the panel: what a person typed (``›``) and what the agent said, in the
# terminal's own colors. Not a vendor screen: no vendor prompt box, spinner or message glyphs.

CONVERSATION = {
    API: (
        [("› ", "dim"), ("Add a rate limit to the login API, test it, and leave a report.", "you")],
        [],
        [("  Before work starts, here is what I understood:", "body")],
        [("    intent     ", "dim"), ("limit login attempts per API key, with tests and a report", "body")],
        [("    scope      ", "dim"), ("src/auth/ and tests/auth/; not the other endpoints", "body")],
        [("    done when  ", "dim"), ("the limiter tests pass and the change report is written", "body")],
        [("  Proceed, Edit, or Stop?", "body")],
        [],
        [("› ", "dim"), ("Proceed", "you")],
        [],
        [("  Route ", "body"), ("code · staged", "dim"), (": plan ✓ › execute ● › test › report", "body")],
        [("  The execute worker edits web-api-wt/execute, on its own branch.", "body")],
        [],
        [("  ", "body"), ("[b2] codex", "codex"), (" finished the pagination review: 2 findings in cursor.py.", "body")],
        [("  I asked ", "body"), ("[b2]", "codex"), (" to re-run the API tests on feat/rate-limit (rq-7d21c4).", "body")],
        [("  Partial report so far: 31 of 40 suites pass, 9 still running.", "body")],
        [],
        [("› ", "dim"), ("▏", "you")],
    ),
    API_NOTES: (
        [("› ", "dim"), ("Draft the 2.4 release notes from the merged pull requests.", "you")],
        [],
        [("  I grouped 23 merged changes into features, fixes and internal work.", "body")],
        [("  Two choices before I write it; they are in the panel below.", "body")],
    ),
    ML: (
        [("› ", "dim"), ("Compare the ASR checkpoints on the 16 kHz subset.", "you")],
        [],
        [("  Route ", "body"), ("lab · staged", "dim"), (": setup ✓ › eval-asr ● › eval-sep › report", "body")],
        [("  Setup prepared 1,200 clips; eval-asr and eval-sep run as separate workers.", "body")],
        [],
        [("  ", "body"), ("[e8] opencode", "opencode"), (": 8 loader workers is the knee, 1.9x over 4.", "body")],
        [],
        [("› ", "dim"), ("▏", "you")],
    ),
}
_CONVERSATION_STYLE = {"you": {"fg": 253, "bold": True}, "body": {"fg": None}, "dim": {"dim": True},
                       "claude": {"fg": 116}, "codex": {"fg": 176}, "opencode": {"fg": 147}}


def conversation(sid, cols, rows, *, top=1):
    """The illustrative conversation of a demo session as panel-shaped rows of parts."""
    out = [[] for _ in range(top)]
    for line in CONVERSATION.get(sid, ()):
        out.append([{"text": (" " + text) if index == 0 else text, **_CONVERSATION_STYLE[key]}
                    for index, (text, key) in enumerate(line)])
    out = out[:rows]
    return out + [[] for _ in range(rows - len(out))]


# --- framing --------------------------------------------------------------------------------
SCREEN = (10, 12, 19)          # the backdrop the windows stand on
EDGE = (43, 46, 60)            # a window's one-pixel edge
BAR = (21, 24, 36)             # a window's title bar
BAR_TEXT = (138, 138, 154)
BAR_H = 26


def window(picture, title):
    """``picture`` inside a plain window frame whose bar names what it shows."""
    from PIL import Image, ImageDraw
    width, height = picture.size
    out = Image.new("RGB", (width + 2, height + BAR_H + 2), EDGE)
    canvas = ImageDraw.Draw(out)
    canvas.rectangle([1, 1, width, BAR_H], fill=BAR)
    for index in range(3):
        canvas.ellipse([10 + index * 15, 9, 19 + index * 15, 18], fill=(47, 51, 67))
    canvas.text((62, 4), title, font=fonts().get("latin"), fill=BAR_TEXT)
    out.paste(picture, (1, BAR_H + 1))
    return out


def screen(rows_of_windows, *, gap=12, pad=14):
    """Windows laid out in rows (each row left to right) on the backdrop."""
    from PIL import Image
    widths = [sum(w.size[0] for w in row) + gap * (len(row) - 1) for row in rows_of_windows]
    heights = [max(w.size[1] for w in row) for row in rows_of_windows]
    canvas = Image.new("RGB", (max(widths) + 2 * pad, sum(heights) + gap * (len(heights) - 1) + 2 * pad), SCREEN)
    y = pad
    for row, width, height in zip(rows_of_windows, widths, heights):
        x = pad + (max(widths) - width) // 2
        for item in row:
            canvas.paste(item, (x, y))
            x += item.size[0] + gap
        y += height + gap
    return canvas


def caption(text, cols, *, key=""):
    """One caption row under a picture: the key in bold, then what it does, dim."""
    row = [{"text": " "}]
    if key:
        row += [{"text": key, "fg": 253, "bold": True}, {"text": "  "}]
    row += [{"text": text, "dim": True}]
    return image(cells([[], row]), cols, 2)


def beside(left, right, cols_left, gap=2):
    """Two grids side by side, the left one padded to ``cols_left`` cells and a ``gap``."""
    out = []
    for index in range(max(len(left), len(right))):
        row = list(left[index]) if index < len(left) else []
        used = sum(cell[-1] for cell in row)
        row += [(" ", None, None, False, False, False, 1)] * max(0, cols_left + gap - used)
        out.append(row + (list(right[index]) if index < len(right) else []))
    return out
